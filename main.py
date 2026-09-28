"""Punto de entrada: `python main.py`. Railway define PORT automáticamente."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
