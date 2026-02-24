import json
import sys


class Document:
    """Query layer over Step 2 JSON output."""

    def __init__(self, data):
        self.filename = data["filename"]
        self.total_pages = data["total_pages"]
        self._blocks = data["blocks"]

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(json.load(f))

    def blocks(self):
        """Return list of (id, title, type) for every block.

        type is one of: text_block, table, image_text, image_table.
        Use get_data(id) to get the block's data.
        """
        return [
            (i, b.get("title") or "", b["type"])
            for i, b in enumerate(self._blocks)
        ]

    def get(self, block_id):
        """Return the full block dict for the given id."""
        if block_id < 0 or block_id >= len(self._blocks):
            raise KeyError(f"No block with id {block_id}")
        return self._blocks[block_id]

    def get_data(self, block_id):
        """Return the data field for the given block (text or table structure)."""
        return self.get(block_id).get("data")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "pdfs/testpdf.json"
    doc = Document.load(path)

    print(f"File: {doc.filename}  ({doc.total_pages} pages)")
    print(f"Total blocks: {len(doc._blocks)}\n")

    print("=== All blocks ===")
    for block_id, title, entity_type in doc.blocks():
        block = doc.get(block_id)
        page = block.get("page", "?")
        data = doc.get_data(block_id)
        if isinstance(data, dict):
            n = len(data.get("rows", [])) or len(data.get("key_value", {}))
            print(f"  [{block_id:3d}] {entity_type:<14s} p{str(page):<6s}  {title}  ({n} items)")
        else:
            print(f"  [{block_id:3d}] {entity_type:<14s} p{str(page):<6s}  {title}")