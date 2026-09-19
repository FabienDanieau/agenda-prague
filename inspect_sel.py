"""Inspect one .event-item block from a saved page, to design a selector-based scraper."""
import sys

from bs4 import BeautifulSoup

path, sel = sys.argv[1], sys.argv[2]
soup = BeautifulSoup(open(path, encoding="utf-8").read(), "lxml")
items = soup.select(sel)
print(f"{len(items)} matches for {sel!r}\n")
for it in items[:3]:
    print(" ".join(str(it).split())[:900])
    print("-" * 70)
