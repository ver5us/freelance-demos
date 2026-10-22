# -*- coding: utf-8 -*-
"""
ParserDemo — сбор каталога книг с тренировочного сайта books.toscrape.com в CSV.

Сайт создан специально для практики парсинга, данные открытые.

Запуск:
    python parser.py              # 2 страницы каталога (до 40 книг)
    python parser.py --pages 5    # больше страниц

Результат: файл books.csv рядом со скриптом + сводка в консоли.
"""
import argparse
import asyncio
import csv
import os

import aiohttp
from bs4 import BeautifulSoup

BASE_URL = "https://books.toscrape.com/catalogue/page-{}.html"
OUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "books.csv")

# Класс звёзд рейтинга на странице -> число
RATING = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


async def fetch(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        return await resp.text()


def parse_page(page_html: str) -> list:
    """Достаёт книги со страницы каталога: название, цена, наличие, рейтинг."""
    soup = BeautifulSoup(page_html, "html.parser")
    books = []
    for card in soup.select("article.product_pod"):
        title = card.h3.a.get("title", "").strip()
        price = card.select_one(".price_color").get_text(strip=True)
        stock = card.select_one(".availability").get_text(strip=True)
        classes = card.p.get("class", [])
        rating = RATING.get(classes[-1], 0) if classes else 0
        books.append(
            {"title": title, "price": price, "availability": stock, "rating": rating}
        )
    return books


async def main(pages: int) -> None:
    all_books = []
    async with aiohttp.ClientSession() as session:
        for page in range(1, pages + 1):
            url = BASE_URL.format(page)
            try:
                page_html = await fetch(session, url)
            except Exception as exc:  # конец каталога или сбой сети
                print(f"Страница {page}: остановка ({type(exc).__name__}).")
                break
            books = parse_page(page_html)
            if not books:
                print(f"Страница {page}: книг не найдено, конец каталога.")
                break
            all_books.extend(books)
            print(f"Страница {page}: собрано книг — {len(books)}")

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        # Разделитель ";": русский Excel раскладывает данные по колонкам
        writer = csv.DictWriter(
            fh, fieldnames=["title", "price", "availability", "rating"], delimiter=";"
        )
        writer.writeheader()
        writer.writerows(all_books)

    print(f"\nГотово: {len(all_books)} книг сохранено в {OUT_CSV}")
    print("Пример первых строк:")
    for book in all_books[:10]:
        print(f"  {book['rating']}*  {book['price']:>10}  {book['title'][:60]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Демо-парсер каталога книг.")
    parser.add_argument("--pages", type=int, default=2, help="сколько страниц каталога собрать")
    args = parser.parse_args()
    asyncio.run(main(args.pages))
