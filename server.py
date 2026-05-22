import os
import io
import httpx
from PIL import Image as PILImage
from fastmcp import FastMCP
from fastmcp.utilities.types import Image

mcp = FastMCP("Image Viewer")


def shrink(url, max_edge=1568):
    data = httpx.get(url, timeout=60, follow_redirects=True).content
    img = PILImage.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((max_edge, max_edge))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


@mcp.tool
def view_asset(url: str) -> Image:
    """Download an image from a URL so it can be viewed and described."""
    return Image(data=shrink(url), format="jpeg")


@mcp.tool
def compare_assets(urls: list[str]) -> list[Image]:
    """Download up to 4 images from URLs so they can be compared side by side."""
    return [Image(data=shrink(u, 1024), format="jpeg") for u in urls[:4]]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    mcp.run(transport="http", host="0.0.0.0", port=port, path="/mcp")
