from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import os
import tempfile
import shutil
from pathlib import Path
import subprocess
import logging
from typing import Dict
import uuid

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Vocal Remover API",
    description="AI-powered vocal separation service",
    version="1.0.0"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Constants
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {".mp3", ".wav"}
TEMP_DIR = Path("/tmp") if os.name != 'nt' else Path(tempfile.gettempdir())

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "Vocal Remover API is running", "status": "healthy"}

@app.get("/health")
async def health_check():
    """Detailed health check"""
    return {
        "status": "healthy",
        "spleeter_available": check_spleeter_installation(),
        "temp_dir": str(TEMP_DIR),
        "max_file_size_mb": MAX_FILE_SIZE // (1024 * 1024)
    }

def check_spleeter_installation() -> bool:
    """Check if Spleeter is properly installed"""
    try:
        result = subprocess.run(
            ["spleeter", "--help"], 
            capture_output=True, 
            text=True, 
            timeout=10
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

def validate_audio_file(file: UploadFile) -> None:
    """Validate uploaded audio file"""
    # Check file extension
    file_ext = Path(file.filename or "").suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format. Allowed formats: {', '.join(ALLOWED_EXTENSIONS)}"
        )
    
    # Check file size (this is approximate since we haven't read the full file yet)
    if hasattr(file, 'size') and file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024 * 1024)}MB"
        )

def run_spleeter_separation(input_file: Path, output_dir: Path) -> Dict[str, Path]:
    """Run Spleeter to separate vocals and accompaniment"""
    try:
        # Run Spleeter with 2stems-16kHz model (vocals + accompaniment)
        cmd = [
            "spleeter",
            "separate",
            "-p", "spleeter:2stems-16kHz",
            "-o", str(output_dir),
            str(input_file)
        ]
        
        logger.info(f"Running Spleeter command: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes timeout
        )
        
        if result.returncode != 0:
            logger.error(f"Spleeter failed: {result.stderr}")
            raise HTTPException(
                status_code=500,
                detail=f"Audio separation failed: {result.stderr}"
            )
        
        # Find the output files
        stem_name = input_file.stem
        output_stem_dir = output_dir / stem_name
        
        vocals_file = output_stem_dir / "vocals.wav"
        accompaniment_file = output_stem_dir / "accompaniment.wav"
        
        if not vocals_file.exists() or not accompaniment_file.exists():
            raise HTTPException(
                status_code=500,
                detail="Separation completed but output files not found"
            )
        
        return {
            "vocals": vocals_file,
            "instrumental": accompaniment_file
        }
        
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=408,
            detail="Processing timeout. Please try with a shorter audio file."
        )
    except Exception as e:
        logger.error(f"Spleeter separation error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Audio separation failed: {str(e)}"
        )

@app.post("/separate")
async def separate_audio(file: UploadFile = File(...)):
    """Separate vocals and instrumental from uploaded audio file"""
    
    # Validate file
    validate_audio_file(file)
    
    # Generate unique session ID
    session_id = str(uuid.uuid4())
    
    # Create temporary directories
    session_temp_dir = TEMP_DIR / f"vocal_remover_{session_id}"
    session_temp_dir.mkdir(exist_ok=True)
    
    input_file = None
    
    try:
        # Save uploaded file
        file_ext = Path(file.filename or "audio.mp3").suffix
        input_file = session_temp_dir / f"input{file_ext}"
        
        # Read and save file content
        content = await file.read()
        
        # Check actual file size
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024 * 1024)}MB"
            )
        
        with open(input_file, "wb") as f:
            f.write(content)
        
        logger.info(f"Processing file: {file.filename} (Session: {session_id})")
        
        # Run Spleeter separation
        separated_files = run_spleeter_separation(input_file, session_temp_dir)
        
        # Return file paths for download
        return {
            "session_id": session_id,
            "vocals_file": f"/download/{session_id}/vocals",
            "instrumental_file": f"/download/{session_id}/instrumental",
            "original_filename": file.filename,
            "message": "Separation completed successfully"
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error during separation: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred: {str(e)}"
        )
    finally:
        # Clean up input file immediately (keep output files for download)
        if input_file and input_file.exists():
            try:
                input_file.unlink()
            except Exception as e:
                logger.warning(f"Failed to clean up input file: {e}")

@app.get("/download/{session_id}/{track_type}")
async def download_track(session_id: str, track_type: str):
    """Download separated audio track"""
    
    if track_type not in ["vocals", "instrumental"]:
        raise HTTPException(status_code=400, detail="Invalid track type")
    
    session_temp_dir = TEMP_DIR / f"vocal_remover_{session_id}"
    
    if not session_temp_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found or expired")
    
    # Find the output directory (should be the only subdirectory)
    output_dirs = [d for d in session_temp_dir.iterdir() if d.is_dir()]
    if not output_dirs:
        raise HTTPException(status_code=404, detail="Processed files not found")
    
    output_dir = output_dirs[0]
    
    if track_type == "vocals":
        file_path = output_dir / "vocals.wav"
        filename = "vocals.wav"
    else:  # instrumental
        file_path = output_dir / "accompaniment.wav"
        filename = "instrumental.wav"
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"{track_type.title()} file not found")
    
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="audio/wav"
    )

@app.delete("/cleanup/{session_id}")
async def cleanup_session(session_id: str):
    """Clean up session files"""
    session_temp_dir = TEMP_DIR / f"vocal_remover_{session_id}"
    
    if session_temp_dir.exists():
        try:
            shutil.rmtree(session_temp_dir)
            return {"message": "Session cleaned up successfully"}
        except Exception as e:
            logger.error(f"Failed to clean up session {session_id}: {e}")
            raise HTTPException(status_code=500, detail="Cleanup failed")
    else:
        return {"message": "Session not found or already cleaned up"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)