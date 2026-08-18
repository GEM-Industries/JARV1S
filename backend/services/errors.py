from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

class AppError(Exception):
    """Base application error."""
    def __init__(
        self, 
        message: str, 
        status_code: int = 500, 
        details: Optional[Dict[str, Any]] = None
    ):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

def setup_error_handlers(app: FastAPI) -> None:
    """Setup basic error handlers for the application."""
    
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        logger.error(
            f"Application error: {exc.message}", 
            extra={
                "status_code": exc.status_code,
                "details": exc.details,
                "path": request.url.path
            }
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.message,
                "details": exc.details,
                "status_code": exc.status_code
            }
        )
    
    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception):
        logger.error(
            f"Unexpected error: {str(exc)}", 
            extra={
                "path": request.url.path,
                "error_type": type(exc).__name__
            },
            exc_info=True
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Internal server error",
                "details": {"message": str(exc)},
                "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR
            }
        ) 