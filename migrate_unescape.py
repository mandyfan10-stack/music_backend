"""Одноразовая миграция: снять HTML-экранирование с уже сохранённых данных.

Раньше сервер хранил поля экранированными (html.escape), а фронтенд экранировал
их повторно при рендере — получалось двойное экранирование («Tom &amp;amp; Jerry»).
Теперь данные хранятся «сырыми», а экранирование выполняется только на выводе.
Этот скрипт приводит существующие документы к новому формату.

Запуск:
    python migrate_unescape.py --dry-run   # показать, что изменится
    python migrate_unescape.py             # применить изменения

Перед запуском без --dry-run сделайте бэкап коллекций releases и reviews.
"""
import argparse
import asyncio
import html
import os

from motor.motor_asyncio import AsyncIOMotorClient

RELEASE_FIELDS = ("name", "artist", "genre", "createdBy")
REVIEW_FIELDS = ("text", "author")


def unescape(value):
    if not isinstance(value, str):
        return value, False
    fixed = html.unescape(value)
    return fixed, fixed != value


async def migrate_collection(col, fields, dry_run):
    changed = 0
    async for doc in col.find():
        updates = {}
        for field in fields:
            new_value, did_change = unescape(doc.get(field))
            if did_change:
                updates[field] = new_value
        if updates:
            changed += 1
            print(f"  {doc.get('id', doc.get('_id'))}: {updates}")
            if not dry_run:
                await col.update_one({"_id": doc["_id"]}, {"$set": updates})
    return changed


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Только показать изменения")
    args = parser.parse_args()

    mongo_url = os.getenv("MONGO_URL")
    if not mongo_url:
        raise SystemExit("MONGO_URL not set")

    client = AsyncIOMotorClient(mongo_url)
    db = client["raper_xxii_database"]
    try:
        print("releases:")
        releases_changed = await migrate_collection(db["releases"], RELEASE_FIELDS, args.dry_run)
        print("reviews:")
        reviews_changed = await migrate_collection(db["reviews"], REVIEW_FIELDS, args.dry_run)
        mode = "would update" if args.dry_run else "updated"
        print(f"\nDone: {mode} {releases_changed} releases, {reviews_changed} reviews.")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
