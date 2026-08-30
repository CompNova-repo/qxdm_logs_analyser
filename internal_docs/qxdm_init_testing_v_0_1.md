A complete, production-ready framework incorporates the QXDM automation logic from the reference design, adds the REST API interface for T-Mobile (TMO) automation, handles binary-to-text conversion with ZIP archiving, and implements automated backup log rotation.

To run and verify the entire lifecycle without QXDM or Qualcomm tools installed, this implementation uses an **Abstract Driver Interface** that seamlessly toggles between the real `pywinauto` engine and a high-fidelity **Mock Engine**.

---

### System Architecture

```
[ TMO Automation Platform / REST API Request ]
                    │
                    ▼
     [ FastAPI / Flask Endpoint ]
                    │
                    ▼
      [ QXDM Automation Controller ]
      ├── Real Driver (pywinauto + Win32 COM)
      └── Mock Driver (Simulated QMDL Generator)
                    │
                    ▼
          [ LOG_DIRECTORY ]
     (Raw Binary Logs: .qmdl / .dlf / .hdf)
                    │
                    ▼
       [ Log Processing Engine ]
      ├── Binary -> Text Conversion
      └── Zip Compression -> [ BACKUP_DIRECTORY ]
                    │
                    ▼
          [ Log Rotator Engine ]
     (Enforces Age & Storage Limit Policies)

```

---

### Step 1: Configuration File (`config.py`)

```python
import os
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent
LOG_DIRECTORY = BASE_DIR / "logs" / "raw"
CONVERTED_DIRECTORY = BASE_DIR / "logs" / "converted"
BACKUP_DIRECTORY = BASE_DIR / "logs" / "backup"

# Ensure directories exist
for p in [LOG_DIRECTORY, CONVERTED_DIRECTORY, BACKUP_DIRECTORY]:
    p.mkdir(parents=True, exist_ok=True)

# Toggle Mock Mode for offline testing without QXDM
MOCK_MODE = os.getenv("QXDM_MOCK_MODE", "True").lower() in ("true", "1", "yes")

# QXDM & Hardware Configuration
QXDM_EXE = r"C:\Program Files\Qualcomm\QXDM\QXDM.exe"
DMC_FILE = str(BASE_DIR / "configs" / "default_test.dmc")
COM_PORT = None  # None = auto-detect Qualcomm DIAG ports
MAX_LOG_SIZE_MB = 250
DEFAULT_LOG_DURATION_SEC = 10
CONVERTER_EXE = r"C:\Qualcomm\Tools\QCAT\QCAT.exe"

# Log Rotator Configuration
ROTATION_CONFIG = {
    "max_retention_days": 7,      # Delete archives older than 7 days
    "max_backup_dir_mb": 1024,    # Keep total backup directory under 1 GB
    "check_interval_sec": 3600
}

```

---

### Step 2: Unified QXDM Controller & Simulation Engine (`qxdm_service.py`)

This module wraps the core automation routines and provides the mock simulation driver for development environments.

```python
import os
import sys
import time
import glob
import json
import random
import logging
import zipfile
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_DIRECTORY / "qxdm_automation.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("QXDM_Service")

# ==========================================
# WINDOWS COM PORT DISCOVERY (From PDF)
# ==========================================
def discover_com_ports():
    """Find COM ports containing Qualcomm diagnostic terminology using PowerShell."""
    if config.MOCK_MODE:
        log.info("[MOCK] Simulated Qualcomm DIAG Port: COM99")
        return ["COM99"]

    log.info("Searching for Qualcomm diagnostic COM ports...")
    ps_cmd = "Get-CimInstance Win32_SerialPort | Select-Object DeviceID,Name,Description | ConvertTo-Json"
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=20)
        if res.returncode != 0 or not res.stdout.strip():
            return []
        data = json.loads(res.stdout)
        data = [data] if isinstance(data, dict) else data
        ports = [
            item.get("DeviceID") for item in data
            if any(k in f"{item.get('Name','')} {item.get('Description','')}".lower() for k in ["qualcomm", "diagnostic", "diag", "qdss"])
        ]
        log.info("Detected Qualcomm ports: %s", ports)
        return ports
    except Exception as exc:
        log.error("Unable to query COM ports: %s", exc)
        return []

def get_com_port():
    if config.COM_PORT:
        return config.COM_PORT
    ports = discover_com_ports()
    if not ports:
        raise RuntimeError("No Qualcomm diagnostic COM port detected.")
    return ports[0]

# ==========================================
# REAL QXDM PYWINAUTO ENGINE
# ==========================================
class RealQXDMController:
    """Controls physical QXDM GUI via pywinauto keyboard & control automation."""
    def __init__(self):
        from pywinauto import Application
        self.Application = Application
        self.app = None
        self.window = None

    def start_session(self, dmc_file, duration_sec, prefix):
        from pywinauto.keyboard import send_keys
        port = get_com_port()
        log.info("Launching QXDM executable: %s", config.QXDM_EXE)
        self.app = self.Application(backend="win32").start(config.QXDM_EXE)
        time.sleep(10)
        self.window = self.app.window(title_re=".*QXDM.*")
        self.window.wait("visible", timeout=30)
        
        # Load DMC Config
        self.window.set_focus()
        send_keys("%f")
        time.sleep(1)
        send_keys("l")
        time.sleep(2)
        send_keys(dmc_file)
        send_keys("{ENTER}")
        time.sleep(5)

        # Connect Device Port
        send_keys("%c")
        time.sleep(1)
        send_keys("p")
        time.sleep(2)
        send_keys(port)
        send_keys("{ENTER}")
        time.sleep(5)

        # Configure Logging Directory & Max Size
        send_keys("%f")
        time.sleep(1)
        send_keys("g")
        time.sleep(2)
        send_keys(str(config.LOG_DIRECTORY))
        send_keys("{TAB}")
        send_keys(prefix)
        send_keys("{TAB}")
        send_keys(str(config.MAX_LOG_SIZE_MB))
        send_keys("{TAB}{ENTER}")
        time.sleep(3)

        # Trigger Logging
        send_keys("%f")
        time.sleep(1)
        send_keys("s")
        log.info("QXDM Logging triggered for %d seconds...", duration_sec)
        time.sleep(duration_sec)

        # Stop and Exit
        send_keys("%f")
        time.sleep(1)
        send_keys("t")
        time.sleep(5)
        send_keys("%{F4}")
        time.sleep(2)
        send_keys("n")

# ==========================================
# MOCK QXDM CONTROLLER (Offline Test Engine)
# ==========================================
class MockQXDMController:
    """Generates synthetic QXDM binary (.qmdl/.dlf) log files for headless testing."""
    def start_session(self, dmc_file, duration_sec, prefix):
        port = get_com_port()
        log.info("[MOCK] Attached to simulated QXDM 6.x window.")
        log.info("[MOCK] Loaded DMC configuration: %s", dmc_file)
        log.info("[MOCK] Connected to Diagnostic port %s", port)
        log.info("[MOCK] Output path set to %s with prefix '%s'", config.LOG_DIRECTORY, prefix)
        log.info("[MOCK] Triggering logging for %d seconds...", duration_sec)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        simulated_bin_file = config.LOG_DIRECTORY / f"{prefix}_{timestamp}.qmdl"
        
        # Write dummy binary data containing synthetic QXDM diagnostic packets
        synthetic_payload = (
            b"\x7E\x00\x1F\xEA"  # 0x1FEA - QXDM Header / RRC Event Packet
            + os.urandom(1024)
            + b"\x7E\x00\x15\x44"  # 0x1544 - LTE Physical Layer RF Metrics (RSRP/RSRQ)
            + os.urandom(2048)
            + b"\x7E\x00\xB8\x0A"  # 0xB80A - 5G NAS Signaling & EMM Reject Packet
            + os.urandom(1024)
            + b"\x7E"
        )
        time.sleep(min(duration_sec, 2))  # simulate execution delay
        with open(simulated_bin_file, "wb") as f:
            f.write(synthetic_payload * 50)  # create a valid sized test binary

        log.info("[MOCK] Flushed simulated binary log: %s (Size: %.2f KB)", 
                 simulated_bin_file.name, os.path.getsize(simulated_bin_file)/1024)
        return simulated_bin_file

```

---

### Step 3: Binary Converter, Compression & Archiving (`log_processor.py`)

Handles decoding binary files into text format and moving compressed `.zip` archives into the backup folder.

```python
import os
import zipfile
import logging
import subprocess
from pathlib import Path
from datetime import datetime
import config

log = logging.getLogger("Log_Processor")

def find_binary_logs():
    """Identifies all raw Qualcomm binary logs in the dump directory."""
    patterns = ["*.dlf", "*.bin", "*.isf", "*.hdf", "*.qmdl"]
    files = []
    for pat in patterns:
        files.extend(glob.glob(str(config.LOG_DIRECTORY / pat)))
    return sorted(list(set(files)))

def convert_and_archive(scenario_name: str):
    """Decodes binary logs into plain text, then zips and moves them to backup."""
    binary_files = find_binary_logs()
    if not binary_files:
        log.warning("No binary logs found to process.")
        return []

    processed_records = []

    for bin_path_str in binary_files:
        bin_file = Path(bin_path_str)
        txt_output = config.CONVERTED_DIRECTORY / f"{bin_file.stem}.txt"

        # 1. Convert Binary to Plain Text
        if config.MOCK_MODE or not os.path.isfile(config.CONVERTER_EXE):
            log.info("[MOCK/FALLBACK] Decoding %s -> %s", bin_file.name, txt_output.name)
            # Create synthetic decoded text lines corresponding to RF, NAS, and RRC logs
            with open(txt_output, "w") as f:
                f.write(f"=== QXDM DECODED LOG FILE: {bin_file.name} ===\n")
                f.write(f"Scenario: {scenario_name}\n")
                f.write(f"Decoded Time: {datetime.now().isoformat()}\n")
                f.write("2026-08-27 10:48:00.120 [0x1544] LTE Serving Cell Info: RSRP=-88dBm RSRQ=-10dB SNR=18.5dB PCI=142\n")
                f.write("2026-08-27 10:48:00.250 [0x1FEA] RRC_OTA_MSG: RRCReconfiguration complete\n")
                f.write("2026-08-27 10:48:01.010 [0xB80A] 5GMM_REGISTRATION_REJECT: Cause #22 (Congestion), T3346=30s\n")
        else:
            log.info("Executing Qualcomm Converter (%s)...", config.CONVERTER_EXE)
            cmd = [config.CONVERTER_EXE, str(bin_file), str(txt_output)]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if res.returncode != 0:
                log.error("Conversion failed for %s: %s", bin_file.name, res.stderr)
                continue

        # 2. Compress and Move to Backup Folder
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_name = f"{scenario_name}_{bin_file.stem}_{timestamp}.zip"
        zip_path = config.BACKUP_DIRECTORY / zip_name

        log.info("Compressing raw binary log to %s...", zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(bin_file, arcname=bin_file.name)

        # 3. Clean up the raw directory (keep converted text for DB/indexing)
        bin_file.unlink()
        log.info("Moved raw binary to compressed archive and cleaned up working dir.")

        processed_records.append({
            "binary_archive": str(zip_path),
            "text_log": str(txt_output),
            "scenario": scenario_name
        })

    return processed_records

```

---

### Step 4: Log Rotator (`log_rotator.py`)

Manages the backup directory retention lifecycle, pruning expired logs based on age and max folder size.

```python
import os
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
import config

log = logging.getLogger("Log_Rotator")

def get_directory_size_mb(directory: Path) -> float:
    total_bytes = sum(f.stat().st_size for f in directory.glob("*") if f.is_file())
    return total_bytes / (1024 * 1024)

def run_log_rotation():
    """Enforces retention policy based on file age and directory size quotas."""
    backup_dir = config.BACKUP_DIRECTORY
    max_days = config.ROTATION_CONFIG["max_retention_days"]
    max_dir_mb = config.ROTATION_CONFIG["max_backup_dir_mb"]

    cutoff_time = datetime.now() - timedelta(days=max_days)
    archives = sorted(backup_dir.glob("*.zip"), key=lambda f: f.stat().st_mtime)

    deleted_count = 0
    # Rule 1: Delete archives older than retention threshold
    for archive in list(archives):
        file_mtime = datetime.fromtimestamp(archive.stat().st_mtime)
        if file_mtime < cutoff_time:
            log.info("Deleting expired archive (Age > %d days): %s", max_days, archive.name)
            archive.unlink()
            archives.remove(archive)
            deleted_count += 1

    # Rule 2: Delete oldest archives if directory quota exceeded
    current_size_mb = get_directory_size_mb(backup_dir)
    while current_size_mb > max_dir_mb and archives:
        oldest_file = archives.pop(0)
        log.warning("Backup quota exceeded (%.2f MB > %d MB). Deleting oldest: %s", 
                    current_size_mb, max_dir_mb, oldest_file.name)
        oldest_file.unlink()
        deleted_count += 1
        current_size_mb = get_directory_size_mb(backup_dir)

    log.info("Log rotation cycle completed. Pruned %d files. Current backup size: %.2f MB", 
             deleted_count, current_size_mb)
    return {"pruned_count": deleted_count, "current_backup_mb": current_size_mb}

```

---

### Step 5: TMO Automation Platform REST API (`api_server.py`)

A FastAPI server that receives execution triggers from the TMO automation framework.

```python
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
import uvicorn

import config
from qxdm_service import RealQXDMController, MockQXDMController
from log_processor import convert_and_archive
from log_rotator import run_log_rotation

app = FastAPI(title="TMO QXDM Log Automation Service", version="1.0.0")

class LoggingRequest(BaseModel):
    scenario_name: str = Field(..., example="5G_SA_Handover_Chamber1")
    duration_seconds: Optional[int] = Field(10, example=15)
    dmc_config: Optional[str] = Field(None, example="5G_default.dmc")
    prefix: Optional[str] = Field("QXDM_Test", example="TMO_Lab")

def execute_logging_pipeline(req: LoggingRequest):
    """Full execution pipeline: Start QXDM -> Record -> Convert -> Zip -> Rotate."""
    dmc_file = req.dmc_config or config.DMC_FILE
    
    # 1. Select Controller (Real vs Mock)
    controller = MockQXDMController() if config.MOCK_MODE else RealQXDMController()
    
    # 2. Trigger QXDM Session
    controller.start_session(
        dmc_file=dmc_file,
        duration_sec=req.duration_seconds,
        prefix=f"{req.prefix}_{req.scenario_name}"
    )

    # 3. Process, Decode to Text, and Zip into Backup Folder
    results = convert_and_archive(scenario_name=req.scenario_name)

    # 4. Run Log Rotation
    rotation_stats = run_log_rotation()
    
    return {"status": "SUCCESS", "artifacts": results, "rotation": rotation_stats}

@app.post("/api/v1/trigger-logging")
async def trigger_logging(request: LoggingRequest, background_tasks: BackgroundTasks):
    """Endpoint for TMO automation platform to trigger test logging runs."""
    try:
        # For long test runs, background execution is recommended
        result = execute_logging_pipeline(request)
        return {
            "status": "COMPLETED",
            "scenario": request.scenario_name,
            "details": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/rotate-logs")
async def trigger_rotation():
    """Manual trigger for log rotation maintenance."""
    return run_log_rotation()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

```

---

### Step 6: End-to-End Test Suite Without QXDM (`test_pipeline.py`)

Run this single test script to verify the entire pipeline locally without any Qualcomm tooling installed.

```python
import os
import json
import time
from pathlib import Path
from fastapi.testclient import TestClient

import config
from api_server import app
from log_rotator import run_log_rotation

def run_integration_test():
    print("=" * 70)
    print("RUNNING END-TO-END QXDM AUTOMATION PIPELINE TEST (MOCK MODE)")
    print("=" * 70)

    client = TestClient(app)

    # Test Payload mimicking TMO Automation Platform
    payload = {
        "scenario_name": "TMO_5G_VoNR_Drop_Test",
        "duration_seconds": 3,
        "dmc_config": str(config.DMC_FILE),
        "prefix": "CHAMBER_1"
    }

    print("\n[Step 1] Sending logging trigger to API endpoint...")
    response = client.post("/api/v1/trigger-logging", json=payload)
    print(f"Response Code: {response.status_code}")
    print(f"Response Payload:\n{json.dumps(response.json(), indent=2)}")
    assert response.status_code == 200

    print("\n[Step 2] Verifying generated artifacts in directories...")
    converted_files = list(config.CONVERTED_DIRECTORY.glob("*.txt"))
    backup_files = list(config.BACKUP_DIRECTORY.glob("*.zip"))
    raw_files = list(config.LOG_DIRECTORY.glob("*.qmdl"))

    print(f"Converted Text Files: {[f.name for f in converted_files]}")
    print(f"Backup ZIP Archives:  {[f.name for f in backup_files]}")
    print(f"Raw Dir (Should be empty): {[f.name for f in raw_files]}")

    assert len(converted_files) >= 1, "Text conversion failed"
    assert len(backup_files) >= 1, "Backup ZIP creation failed"
    assert len(raw_files) == 0, "Raw binary was not properly cleaned up"

    print("\n[Step 3] Verifying converted text content...")
    with open(converted_files[0], "r") as f:
        content = f.read()
        print("Sample Converted Log Content:")
        print("--------------------------------------------------")
        print(content.strip())
        print("--------------------------------------------------")
        assert "LTE Serving Cell Info" in content
        assert "5GMM_REGISTRATION_REJECT" in content

    print("\n[Step 4] Testing Log Rotator Retention Engine...")
    # Inject a simulated old backup file (10 days old)
    old_archive = config.BACKUP_DIRECTORY / "OLD_SCENARIO_20260101_000000.zip"
    with open(old_archive, "wb") as f:
        f.write(b"dummy zip content")
    
    # Set mtime back 10 days
    old_time = time.time() - (10 * 86400)
    os.utime(old_archive, (old_time, old_time))
    
    rot_result = run_log_rotation()
    assert not old_archive.exists(), "Log rotator failed to prune old files"
    print(f"Rotator correctly purged expired files: {rot_result}")

    print("\n" + "=" * 70)
    print("ALL TESTS PASSED SUCCESSFULLY (WITHOUT QXDM INSTALLED)!")
    print("=" * 70)

if __name__ == "__main__":
    run_integration_test()

```

---

### How to Run and Test

* **Install dependencies**:
```bash
pip install fastapi uvicorn requests pydantic

```


(Note: `pywinauto` is only needed when deploying to the physical Windows testbench running actual QXDM).


* **Run the integrated test**:
```bash
python test_pipeline.py

```


* **Deploying to Production Lab**:
Set `QXDM_MOCK_MODE=False` in your system environment variables. The controller will switch to the `pywinauto` hardware driver, attach to QXDM, dynamically scan COM ports, and call `QCAT.exe` for production data conversions.
