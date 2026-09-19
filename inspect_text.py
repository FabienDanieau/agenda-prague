"""Print the visible text of a saved page's main content, to see what a scraper would get."""
import sys

from bs4 import BeautifulSoup

soup = BeautifulSoup(open(sys.argv[1], encoding="utf-8").read(), "lxml")
node = soup.select_one(sys.argv[2] if len(sys.argv) > 2 else "body")
for tag in node.select("script, style, nav, footer"):
    tag.decompose()
print(node.get_text("\n", strip=True)[:1500])
print("\n--- images in that block:")
for img in node.select("img")[:8]:
    print(" ", (img.get("src") or "")[-70:])
