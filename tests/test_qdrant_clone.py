"""Copying a class template reuses indexed vectors instead of re-embedding."""

from types import SimpleNamespace

from app.services.vector_store.qdrant_store import clone_document_points


class _FakeClient:
    def __init__(self):
        self.upserted = []

    def collection_exists(self, name):
        return True

    def scroll(self, **kwargs):
        if kwargs["offset"] is None:
            return (
                [
                    SimpleNamespace(
                        payload={
                            "page_content": "lesson",
                            "metadata": {"document_id": "source", "class_id": "old"},
                        },
                        vector=[0.1, 0.2],
                    )
                ],
                "next",
            )
        return [], None

    def upsert(self, collection_name, points, wait):
        self.upserted = points


def test_clone_document_points_reuses_vectors_under_new_ids():
    client = _FakeClient()

    count = clone_document_points(
        "source", "target", "class-new", client=client, collection_name="documents"
    )

    assert count == 1
    point = client.upserted[0]
    assert point.vector == [0.1, 0.2]
    assert point.payload["page_content"] == "lesson"
    assert point.payload["metadata"] == {"document_id": "target", "class_id": "class-new"}
    assert point.id != "source"
