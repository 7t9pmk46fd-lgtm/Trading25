"""
Review unreviewed research notes from the learning agent.

Usage:
    python scripts/review_notes.py            # list all unreviewed notes
    python scripts/review_notes.py --interactive   # step through and tag each one
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from shared import db


def list_unreviewed():
    with db.db_session() as conn:
        notes = db.get_unreviewed_notes(conn)

    if not notes:
        print("No unreviewed notes.")
        return

    for n in notes:
        print(f"\n#{n['id']} [{n['source_type']}] {n['source_title'] or n['source_url']}")
        print(f"  URL: {n['source_url']}")
        print(f"  Specificity: {n['specificity']}")
        print(f"  Tickers: {n['tickers_mentioned'] or '(none)'}")
        print(f"  Summary: {n['strategy_summary']}")
        if n["raw_excerpt"]:
            print(f"  Excerpt: \"{n['raw_excerpt']}\"")


def interactive_review():
    with db.db_session() as conn:
        notes = db.get_unreviewed_notes(conn)

        if not notes:
            print("No unreviewed notes.")
            return

        for n in notes:
            print(f"\n#{n['id']} [{n['source_type']}] {n['source_title'] or n['source_url']}")
            print(f"  Specificity: {n['specificity']} | Tickers: {n['tickers_mentioned'] or '(none)'}")
            print(f"  Summary: {n['strategy_summary']}")
            choice = input("  Mark as (p)romising / (d)iscard / (s)kip? ").strip().lower()

            if choice == "p":
                notes_text = input("  Optional notes: ").strip()
                conn.execute(
                    "UPDATE research_notes SET status='reviewed_promising', reviewer_notes=?, updated_at=datetime('now') WHERE id=?",
                    (notes_text, n["id"]),
                )
            elif choice == "d":
                conn.execute(
                    "UPDATE research_notes SET status='reviewed_discarded', updated_at=datetime('now') WHERE id=?",
                    (n["id"],),
                )
            # 's' or anything else: leave as unreviewed


if __name__ == "__main__":
    if "--interactive" in sys.argv:
        interactive_review()
    else:
        list_unreviewed()
