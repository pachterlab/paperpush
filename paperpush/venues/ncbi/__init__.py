"""Shared NCBI authentication for services using the NCBI account gateway."""

from .main import NCBILoginError, PROVIDERS, is_logged_in_ncbi, login_ncbi

__all__ = ["NCBILoginError", "PROVIDERS", "is_logged_in_ncbi", "login_ncbi"]
