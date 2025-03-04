#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MPD-Takeaway Lite (mpdtl)
-------------------------
Single script to automatically copy the currently playing MPD album
to a connected USB device on Raspberry Pi.

Based on MPD-Takeaway project v0.8.0
https://github.com/thestreamdigger/mpd-takeaway
"""

import os
import sys
import time
import logging
import json
import shutil
import subprocess
from dataclasses import dataclass
from functools import wraps
from typing import List, Dict, Tuple, Optional, Callable
import psutil

try:
    from mpd import MPDClient as BaseMPDClient
except ImportError:
    print("[ERROR] python-mpd2 library not installed. Run: pip install python-mpd2 psutil")
    sys.exit(1)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
CONFIG = {
    "mpd": {
        "host": "localhost",
        "port": 6600,
        "timeout": 10
    },
    "copy": {
        "min_usb_size_gb": 4,
        "safety_margin_mb": 200,
        "path_structure": {
            "music_root": "/var/lib/mpd/music"
        },
        "metadata_dirs": ["Artwork", "Covers", "Scans", ".thumbnails", "Booklets"]
    },
    "performance": {
        "buffer_size_mb": 16,
        "flush_interval_mb": 128,
        "min_flush_interval_sec": 0.5,
        "large_file_threshold_mb": 10,
        "progress_update_interval_sec": 0.1
    },
    "logging": {
        "level": "INFO",
        "format": "[{level}] {message}"
    }
}

# Version settings
VERSION = "0.1.0"
BASED_ON = "MPD-Takeaway v0.8.0"
SCRIPT_NAME = "MPD-Takeaway Lite (mpdtl)"

# ==============================================================================
# LOGGING SYSTEM
# ==============================================================================
OK_LEVEL = 25
logging.addLevelName(OK_LEVEL, 'OK')
os.makedirs('logs', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/mpdtl.log')
    ]
)

class Logger:
    @staticmethod
    def set_config(config):
        if "logging" in config:
            level = getattr(logging, config["logging"].get("level", "INFO"))
            log_format = config["logging"].get("format", "[{level}] {message}")
            formatter = logging.Formatter(
                log_format.format(
                    level="%(levelname)s",
                    message="%(message)s"
                )
            )
            for handler in logging.getLogger().handlers:
                handler.setFormatter(formatter)
            logging.getLogger().setLevel(level)

    @staticmethod
    def debug(message): logging.debug(message)
    @staticmethod
    def info(message): logging.info(message)
    @staticmethod
    def warning(message): logging.warning(message)
    @staticmethod
    def error(message): logging.error(message)
    @staticmethod
    def ok(message): logging.log(OK_LEVEL, message)

log = Logger()

# ==============================================================================
# OPERATION PROGRESS
# ==============================================================================
@dataclass
class OperationProgress:
    total_items: int = 0
    processed_items: int = 0
    current_item: str = ""
    status: str = "starting"
    error: Optional[str] = None
    start_time: float = 0.0
    last_update: float = 0.0
    bytes_processed: int = 0
    last_bytes: int = 0
    last_speed_update: float = 0.0
    speed_samples: list = None
    
    def __post_init__(self):
        self.speed_samples = []
        self.start_time = time.time()
        self.last_speed_update = time.time()
    
    @property
    def elapsed_time(self) -> float:
        if self.start_time == 0:
            return 0
        return time.time() - self.start_time
    
    @property
    def progress_percentage(self) -> float:
        if self.total_items == 0:
            return 0
        return (self.processed_items / self.total_items) * 100
    
    @property
    def transfer_rate(self) -> float:
        current_time = time.time()
        if current_time - self.last_speed_update >= 0.5:
            bytes_delta = self.bytes_processed - self.last_bytes
            time_delta = current_time - self.last_speed_update
            
            if time_delta > 0:
                current_speed = bytes_delta / time_delta
                self.speed_samples.append(current_speed)
                if len(self.speed_samples) > 5:
                    self.speed_samples.pop(0)
            
            self.last_bytes = self.bytes_processed
            self.last_speed_update = current_time
        
        if self.speed_samples:
            return sum(self.speed_samples) / len(self.speed_samples)
        return 0
    
    @property
    def estimated_remaining(self) -> float:
        if self.progress_percentage == 0 or not self.speed_samples:
            return 0
        current_speed = self.transfer_rate
        if current_speed > 0:
            return (self.elapsed_time / self.progress_percentage) * (100 - self.progress_percentage)
        return 0

class ProgressManager:
    def __init__(self):
        self._progress = OperationProgress()
    
    def set_total(self, total: int):
        self._progress.total_items = total
        
    def increment(self, item_name: str = "", bytes_processed: int = 0):
        self._progress.processed_items += 1
        self._progress.current_item = item_name
        if bytes_processed > 0:
            self._progress.bytes_processed += bytes_processed

# ==============================================================================
# FORMATTING FUNCTIONS
# ==============================================================================
def format_progress_bar(percentage: float, width: int = 50) -> str:
    filled = int(width * percentage / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"

def format_size(size_bytes: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

def format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.1f}s"
    else:
        hours = int(seconds / 3600)
        minutes = int((seconds % 3600) / 60)
        secs = seconds % 60
        return f"{hours}h {minutes}m {secs:.1f}s"

def print_copy_header(src: str, dst: str, total_files: int, total_size: int):
    width = 80
    print("\n" + "=" * width)
    print(f" COPYING ALBUM ".center(width, "="))
    print("=" * width)
    print(f" Source: {src}")
    print(f" Destination: {dst}")
    print(f" Files: {total_files}, Total Size: {format_size(total_size)}")
    print("=" * width + "\n")

def print_copy_summary(total_files: int, total_size: int, elapsed_time: float, rate: str = "", success: bool = True):
    width = 80
    print("\n" + "=" * width)
    if success:
        print(f" COPY COMPLETED SUCCESSFULLY ".center(width, "="))
    else:
        print(f" COPY FINISHED WITH ERRORS ".center(width, "="))
    print("=" * width)
    print(f" Files copied: {total_files}")
    print(f" Total size: {format_size(total_size)}")
    print(f" Total time: {format_time(elapsed_time)}")
    if rate:
        print(f" Average rate: {rate}")
    print("=" * width + "\n")

def print_progress(progress: OperationProgress, current_file: str = "", file_size: int = 0):
    term_width = shutil.get_terminal_size().columns
    progress_width = min(50, max(20, term_width - 30))
    
    if term_width < 80:
        # Simplified version for small terminals
        percentage = progress.progress_percentage
        progress_bar = format_progress_bar(percentage, progress_width)
        progress_text = f"{percentage:.1f}% "
        
        print(f"\r{progress_text}{progress_bar} ", end="", flush=True)
        return
        
    # Full version for larger terminals
    percentage = progress.progress_percentage
    progress_bar = format_progress_bar(percentage, progress_width)
    
    # Current file information
    if current_file:
        file_name = os.path.basename(current_file)
        # Truncate filename if too long
        max_file_len = max(10, term_width - 70)
        if len(file_name) > max_file_len:
            file_name = file_name[:max_file_len-3] + "..."
    else:
        file_name = ""
    
    # Progress information
    progress_text = f"{progress.processed_items}/{progress.total_items} ({percentage:.1f}%)"
    
    # Transfer rate
    rate = progress.transfer_rate
    rate_text = f"{format_size(rate)}/s" if rate > 0 else "-- KB/s"
    
    # Estimated time
    est_remain = progress.estimated_remaining
    eta_text = f"ETA: {format_time(est_remain)}" if est_remain > 0 else "ETA: --"
    
    # Current file size
    size_text = f"[{format_size(file_size)}]" if file_size > 0 else ""
    
    status_line = f"\r{progress_text} {progress_bar} {rate_text} {eta_text}"
    if file_name:
        file_info = f" {file_name} {size_text}"
        # Truncate if necessary to fit on the line
        available_width = term_width - len(status_line) - 1
        if len(file_info) > available_width:
            file_info = file_info[:available_width-3] + "..."
        status_line += file_info
        
    # Clear line and print status
    print(status_line, end="", flush=True)

# ==============================================================================
# MPD CLIENT
# ==============================================================================
class MPDClient:
    def __init__(self, host='localhost', port=6600, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._client = BaseMPDClient()
        self._connected = False
        self._last_try = 0
        self._retry_interval = 5
        self._max_retries = 3
        log.debug(f"MPD Client initialized for {host}:{port}")

    def _handle_connection(self, operation):
        """Manages connection and reconnection attempts for MPD operations"""
        retries = 0
        while retries < self._max_retries:
            if not self._connected:
                log.debug(f"Not connected to MPD, trying to connect (attempt {retries+1}/{self._max_retries})")
                if not self.connect():
                    retries += 1
                    log.debug(f"Connection attempt {retries} failed, waiting before trying again...")
                    time.sleep(1)
                    continue
            try:
                log.debug(f"Executing MPD operation (attempt {retries+1}/{self._max_retries})")
                result = operation(self._client)
                return result
            except ConnectionError as e:
                self._connected = False
                log.error(f"MPD connection error: {str(e)}")
                try:
                    self._client.disconnect()
                except:
                    pass
                retries += 1
                if retries >= self._max_retries:
                    log.error(f"MPD operation failed after {retries} attempts: {str(e)}")
                    return None

    def connect(self):
        """Establishes connection with the MPD server"""
        if self._connected:
            return True
        
        current_time = time.time()
        if current_time - self._last_try < self._retry_interval:
            log.debug("Connection attempt too fast, waiting for retry interval")
            return False
        
        self._last_try = current_time
        
        try:
            log.debug(f"Calling MPD connect('{self.host}', {self.port}, timeout={self.timeout})")
            self._client.connect(self.host, self.port, self.timeout)
            version = self._client.mpd_version
            log.debug(f"Connected to MPD version {version}")
            self._connected = True
            return True
        except ConnectionRefusedError:
            log.error(f"Connection refused by MPD server at {self.host}:{self.port}")
            return False
        except Exception as e:
            log.error(f"Error connecting to MPD: {str(e)}")
            return False

    def get_status(self):
        """Gets the current MPD status"""
        return self._handle_connection(lambda client: client.status())

    def get_current_song(self):
        """Gets information about the currently playing song"""
        return self._handle_connection(lambda client: client.currentsong())

    def close(self):
        """Closes connection with the MPD server"""
        if self._connected:
            try:
                log.debug("Calling MPD disconnect()")
                self._client.close()
                self._client.disconnect()
                self._connected = False
                log.debug("Disconnected from MPD server")
            except:
                pass

# ==============================================================================
# STORAGE FUNCTIONS
# ==============================================================================
def is_usb_device(device_name: str) -> bool:
    """Checks if a device is a removable USB device"""
    try:
        sys_block_path = f"/sys/block/{device_name}"
        log.debug(f"Checking if {device_name} is a USB device")
        
        if not os.path.exists(sys_block_path):
            log.debug(f"Path does not exist: {sys_block_path}")
            return False
        
        removable_path = os.path.join(sys_block_path, "removable")
        
        if os.path.exists(removable_path):
            with open(removable_path, 'r') as f:
                removable_value = f.read().strip()
                if removable_value != "1":
                    log.debug(f"Device {device_name} is not removable")
                    return False
        
        device_path = os.path.join(sys_block_path, "device")
        
        if not os.path.exists(device_path):
            log.debug(f"Device path does not exist: {device_path}")
            return False
        
        try:
            real_path = os.path.realpath(device_path)
            
            if "usb" in real_path:
                log.debug(f"Device {device_name} confirmed as USB device (path contains 'usb')")
                return True
                
            for root, dirs, files in os.walk(device_path):
                for d in dirs:
                    if "usb" in d.lower():
                        log.debug(f"Device {device_name} confirmed as USB device (directory contains 'usb')")
                        return True
        except Exception as e:
            log.debug(f"Error checking USB path for {device_name}: {str(e)}")
        
        log.debug(f"Device {device_name} is considered a USB device (removable disk)")
        return True
    except Exception as e:
        log.debug(f"Error checking if {device_name} is USB: {str(e)}")
        return False

def find_usb_drive(min_size_gb=4):
    """
    Finds a connected USB drive with specified minimum size
    
    Args:
        min_size_gb: Minimum size in GB the drive should have
        
    Returns:
        Tuple (mount_point, usb_info) or (None, None) if not found
    """
    min_size_bytes = min_size_gb * 1024 * 1024 * 1024
    
    log.debug(f"Looking for USB device with at least {min_size_gb} GB")
    
    # Get list of partitions
    partitions = psutil.disk_partitions(all=True)
    
    # List of typical filesystems for USB drives
    usb_filesystems = ['vfat', 'exfat', 'ntfs', 'ext4', 'ext3', 'ext2']
    
    found_usb_drives = []
    
    for partition in partitions:
        device = partition.device
        mountpoint = partition.mountpoint
        fstype = partition.fstype
        
        # Skip partitions without a mount point
        if not mountpoint or not os.path.isdir(mountpoint):
            continue
            
        # Check device
        device_name = os.path.basename(device.rstrip('0123456789'))
        
        log.debug(f"Evaluating partition: {device} at {mountpoint} ({fstype})")
        
        # Check if it's a USB device
        is_usb = False
        try:
            is_usb = is_usb_device(device_name)
        except Exception as e:
            log.debug(f"Error checking USB device: {e}")
            
        # If not USB or if the filesystem is not typical of USB, skip
        if not is_usb and fstype.lower() not in usb_filesystems:
            log.debug(f"Skipping device {device}: doesn't appear to be USB")
            continue
            
        # Check size and free space
        try:
            total = psutil.disk_usage(mountpoint).total
            free = psutil.disk_usage(mountpoint).free
            
            if total < min_size_bytes:
                log.debug(f"Device {device} too small: {format_size(total)} < {min_size_gb} GB")
                continue
                
            log.debug(f"USB found: {device} mounted at {mountpoint}, {format_size(total)} total, {format_size(free)} free")
            
            usb_info = {
                'device': device,
                'mountpoint': mountpoint,
                'fstype': fstype,
                'total_size': total,
                'free_space': free
            }
            
            found_usb_drives.append((mountpoint, usb_info))
        except Exception as e:
            log.debug(f"Error checking space at {mountpoint}: {e}")
            
    # Sort by free space (largest to smallest)
    found_usb_drives.sort(key=lambda x: x[1]['free_space'], reverse=True)
    
    if found_usb_drives:
        log.debug(f"Selected USB drive: {found_usb_drives[0][0]}")
        return found_usb_drives[0]
    else:
        log.debug("No suitable USB device found")
        return None, None

def get_directory_size(path, metadata_dirs=None):
    """
    Calculates the total size of a directory and the number of files,
    excluding metadata directories if specified
    
    Args:
        path: Directory path to analyze
        metadata_dirs: List of directory names to ignore
        
    Returns:
        Tuple (total_size, total_files)
    """
    total_size = 0
    total_files = 0
    
    for root, dirs, files in os.walk(path):
        # Filter out metadata directories if specified
        if metadata_dirs:
            dirs[:] = [d for d in dirs if not is_metadata_dir(os.path.join(root, d), metadata_dirs)]
        
        total_files += len(files)
        for f in files:
            file_path = os.path.join(root, f)
            try:
                total_size += os.path.getsize(file_path)
            except:
                pass
                
    return total_size, total_files

# ==============================================================================
# UTILITY FUNCTIONS
# ==============================================================================
def sanitize_filename(filename: str) -> str:
    """Sanitizes a filename by replacing invalid characters with underscores"""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename.strip()

def is_metadata_dir(dir_path: str, metadata_dirs: list) -> bool:
    """Checks if a directory is metadata that should be ignored"""
    dir_name = os.path.basename(dir_path)
    return dir_name in metadata_dirs

# ==============================================================================
# MAIN CLASS
# ==============================================================================
class AlbumCopier:
    def __init__(self, config):
        self.config = config
        self.mpd_client = MPDClient(
            host=config.get("mpd", {}).get("host", "localhost"),
            port=config.get("mpd", {}).get("port", 6600),
            timeout=config.get("mpd", {}).get("timeout", 10)
        )
        self.progress = ProgressManager()

    def _prepare_destination(self, usb_mount: str, artist_name: str, album_name: str) -> str:
        """Prepares the destination directory on the USB"""
        artist_folder = os.path.join(usb_mount, sanitize_filename(artist_name))
        album_folder = os.path.join(artist_folder, sanitize_filename(album_name))
        os.makedirs(album_folder, exist_ok=True)
        return album_folder

    def copy_current_album(self):
        """Copies the currently playing album to the USB drive"""
        print(f"\n{SCRIPT_NAME} v{VERSION}")
        print(f"Based on {BASED_ON}\n")
        
        song = self.mpd_client.get_current_song()
        if not song:
            log.error("No playing song found.")
            return False
        
        log.debug("Current song tags:")
        for tag, value in song.items():
            log.debug(f"  {tag}: {value}")
        
        relative_file = song.get("file")
        if not relative_file:
            log.error("Current song has no file path.")
            return False

        album_relative = os.path.dirname(relative_file)
        if album_relative == ".":
            log.error("Could not determine album folder from file path.")
            return False

        music_root = self.config.get("copy", {}).get("path_structure", {}).get("music_root")
        if not music_root:
            log.error("Music root path not defined in configuration.")
            return False

        src_album_folder = os.path.join(music_root, album_relative)
        if not os.path.isdir(src_album_folder):
            log.error(f"Album folder does not exist: {src_album_folder}")
            return False

        usb_mount, usb_info = find_usb_drive(min_size_gb=self.config.get("copy", {}).get("min_usb_size_gb", 4))
        if not usb_mount:
            log.error("No eligible USB drive found.")
            return False

        try:
            album_name = song.get("album", "Unknown Album")
            artist_name = song.get("albumartist", song.get("artist", "Unknown Artist"))
            album_date = song.get("date", "")
            
            display_name = f"{album_name} ({album_date})" if album_date else album_name
            
            dst_album_folder = self._prepare_destination(usb_mount, artist_name, display_name)
            
            log.info(f"Copying album '{display_name}' by {artist_name}...")
            log.debug(f"From {src_album_folder} to {dst_album_folder}")
            
            metadata_dirs = self.config.get("copy", {}).get("metadata_dirs", [])
            total_size, total_files = get_directory_size(src_album_folder, metadata_dirs)
            self.progress.set_total(total_files)
            
            print_copy_header(src_album_folder, dst_album_folder, total_files, total_size)
            
            # Loading performance settings
            performance_config = self.config.get("performance", {})
            BUFFER_SIZE = performance_config.get("buffer_size_mb", 16) * 1024 * 1024
            FLUSH_INTERVAL = performance_config.get("flush_interval_mb", 128) * 1024 * 1024
            MIN_FLUSH_INTERVAL = performance_config.get("min_flush_interval_sec", 0.5)
            LARGE_FILE_THRESHOLD = performance_config.get("large_file_threshold_mb", 10) * 1024 * 1024
            PROGRESS_UPDATE_INTERVAL = performance_config.get("progress_update_interval_sec", 0.1)
            
            last_flush_time = time.time()
            last_progress_update = time.time()
            bytes_since_flush = 0
            
            for root, dirs, files in os.walk(src_album_folder):
                # Filter metadata directories
                dirs[:] = [d for d in dirs if not is_metadata_dir(os.path.join(root, d), metadata_dirs)]
                
                rel_path = os.path.relpath(root, src_album_folder)
                dst_dir = os.path.join(dst_album_folder, rel_path)
                os.makedirs(dst_dir, exist_ok=True)
                
                for file in files:
                    src_file = os.path.join(root, file)
                    dst_file = os.path.join(dst_dir, file)
                    
                    try:
                        file_size = os.path.getsize(src_file)
                        print_progress(self.progress._progress, file, file_size)
                        
                        current_buffer = BUFFER_SIZE if file_size >= LARGE_FILE_THRESHOLD else BUFFER_SIZE // 2
                        
                        with open(src_file, 'rb') as fsrc, open(dst_file, 'wb', buffering=current_buffer) as fdst:
                            copied = 0
                            while True:
                                buf = fsrc.read(current_buffer)
                                if not buf:
                                    break
                                fdst.write(buf)
                                copied += len(buf)
                                bytes_since_flush += len(buf)
                                self.progress._progress.bytes_processed += len(buf)
                                
                                current_time = time.time()
                                if current_time - last_progress_update >= PROGRESS_UPDATE_INTERVAL:
                                    print_progress(self.progress._progress, file, file_size)
                                    last_progress_update = current_time
                                
                                if (bytes_since_flush >= FLUSH_INTERVAL and 
                                    current_time - last_flush_time >= MIN_FLUSH_INTERVAL):
                                    fdst.flush()
                                    os.fsync(fdst.fileno())
                                    bytes_since_flush = 0
                                    last_flush_time = current_time
                                    
                                    if file_size >= LARGE_FILE_THRESHOLD:
                                        time.sleep(0.005)
                                    else:
                                        time.sleep(0.001)
                                
                        self.progress.increment(file, file_size)
                        log.debug(f"Copied: {file} ({format_size(file_size)})")
                    except Exception as e:
                        log.error(f"Error copying {file}: {e}")
                        print_copy_summary(total_files, total_size, self.progress._progress.elapsed_time, "", success=False)
                        return False
            
            print_copy_summary(total_files, total_size, self.progress._progress.elapsed_time, "", success=True)
            return True
            
        except Exception as e:
            log.error(f"Error copying album: {e}")
            if 'total_files' in locals() and 'total_size' in locals():
                print_copy_summary(total_files, total_size, self.progress._progress.elapsed_time, "", success=False)
            return False
        finally:
            self.mpd_client.close()

# ==============================================================================
# MAIN FUNCTION
# ==============================================================================
def main():
    try:
        # Configure logger
        Logger.set_config(CONFIG)
        
        # Copy current album
        copier = AlbumCopier(CONFIG)
        success = copier.copy_current_album()
        
        # Exit with appropriate code
        sys.exit(0 if success else 1)
        
    except KeyboardInterrupt:
        print("\nOperation canceled by user.")
        sys.exit(130)
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 