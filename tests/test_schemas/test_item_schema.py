from __future__ import annotations

import pytest

from app.schemas.item import ItemCreate, ItemUpdate, strip_url_query


@pytest.mark.parametrize("schema_cls", [ItemCreate, ItemUpdate])
def test_product_url_query_string_is_stripped_on_save(schema_cls) -> None:
    item = schema_cls(
        name="Item",
        shop_name="Shop",
        product_url="https://booth.pm/ja/items/8583087?utm_source=chatgpt.com",
    )

    assert item.product_url == "https://booth.pm/ja/items/8583087"


@pytest.mark.parametrize("schema_cls", [ItemCreate, ItemUpdate])
def test_product_url_without_a_query_string_is_left_unchanged(schema_cls) -> None:
    item = schema_cls(name="Item", shop_name="Shop", product_url="https://booth.pm/ja/items/8583087")

    assert item.product_url == "https://booth.pm/ja/items/8583087"


@pytest.mark.parametrize("schema_cls", [ItemCreate, ItemUpdate])
def test_product_url_none_is_left_unchanged(schema_cls) -> None:
    item = schema_cls(name="Item", shop_name="Shop", product_url=None)

    assert item.product_url is None


def test_strip_url_query_drops_query_and_fragment() -> None:
    assert (
        strip_url_query("https://booth.pm/ja/items/1?utm_source=x&utm_medium=y#section")
        == "https://booth.pm/ja/items/1"
    )


def test_strip_url_query_leaves_non_url_strings_unchanged() -> None:
    assert strip_url_query("not-a-url?with=query") == "not-a-url?with=query"
