"""Run the chapter's trusted teaching SQL, not arbitrary model-generated SQL."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import sqlite3
from typing import NamedTuple
import unittest


CHAPTER = Path(__file__).resolve().parents[2] / (
    "docs/rag/07-structured-queries/22-text-to-sql.md"
)


class Order(NamedTuple):
    tenant_id: str
    order_id: str
    customer_id: str
    created_at: str
    status: str
    total_amount_cents: int
    promised_at: str | None


class Line(NamedTuple):
    tenant_id: str
    order_id: str
    line_id: int
    ordered_qty: int
    shipped_qty: int
    unit_price_cents: int


def section(text, number):
    match = re.search(
        rf"^## {re.escape(number)} .*?\n(.*?)(?=^## |\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ValueError(f"Missing chapter section {number}")
    return match.group(1)


def sql_block(text):
    blocks = re.findall(r"^```sql\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    if len(blocks) != 1:
        raise ValueError("Expected exactly one SQL block in the section")
    return blocks[0]


def data_tables(text):
    """Read this chapter's simple pipe tables, excluding headers and separators."""
    blocks = re.findall(r"(?:^\|[^\n]*\|\n)+", text, re.MULTILINE)
    return [
        [[cell.strip() for cell in row.strip().strip("|").split("|")]
         for row in block.splitlines()[2:]]
        for block in blocks
    ]


@dataclass
class Example:
    ddl: str
    query: str
    orders: list[Order]
    lines: list[Line]
    bindings: dict[str, str]
    expected: list[tuple[str, int, int, int]]


def read_example(path):
    text = path.read_text(encoding="utf-8")
    schema = section(text, "22.3")
    headers, dates, details = data_tables(schema)
    dates_by_key = {key: (created, promised) for key, created, promised in dates}
    if len(dates_by_key) != len(dates) or set(dates_by_key) != {
        row[0] for row in headers
    }:
        raise ValueError("Order tables must have matching, unique keys")
    orders = []
    for key, customer, status, amount in headers:
        tenant, order_id = key.split("/")
        created, promised = dates_by_key[key]
        orders.append(Order(
            tenant, order_id, customer, created, status, int(amount),
            None if promised == "NULL" else promised,
        ))
    lines = []
    for key, ordered, shipped, price in details:
        tenant, order_id, line_id = key.split("/")
        lines.append(Line(
            tenant, order_id, int(line_id), int(ordered), int(shipped), int(price),
        ))
    bindings = {}
    for names, values, _ in data_tables(section(text, "22.4"))[-1]:
        names = re.findall(r"`([^`]+)`", names)
        values = re.findall(r"`([^`]+)`", values)
        if not names or len(names) != len(values):
            raise ValueError("Expected paired binding names and values")
        bindings.update(zip(names, values))
    query_section = section(text, "22.5")
    [result_rows] = data_tables(query_section)
    expected = [(row[0], *(int(value) for value in row[1:])) for row in result_rows]
    return Example(sql_block(schema), sql_block(query_section), orders, lines,
                   bindings, expected)


def reference_result(orders, lines, bindings):
    """Compute from the business definitions without executing or interpreting SQL."""
    start = datetime.fromisoformat(bindings["start_utc"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(bindings["end_utc"].replace("Z", "+00:00"))
    customers = {bindings["customer_a"], bindings["customer_b"]}
    totals = {}
    for order in orders:
        created = datetime.fromisoformat(order.created_at.replace("Z", "+00:00"))
        if (order.tenant_id != bindings["tenant_id"]
                or order.customer_id not in customers
                or order.status == "cancelled"
                or not start <= created < end):
            continue
        quantity, amount = 0, 0
        for line in lines:
            if (line.tenant_id, line.order_id) != (order.tenant_id, order.order_id):
                continue
            remaining = line.ordered_qty - line.shipped_qty
            quantity += remaining
            amount += remaining * line.unit_price_cents
        if quantity > 0:
            customer = totals.setdefault(order.customer_id, [0, 0, 0])
            customer[0] += 1
            customer[1] += quantity
            customer[2] += amount
    return [(customer, *totals[customer]) for customer in sorted(totals)]


def execute(example, query=None):
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(example.ddl)
        connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                               example.orders)
        connection.executemany("INSERT INTO order_lines VALUES (?, ?, ?, ?, ?, ?)",
                               example.lines)
        return connection.execute(
            example.query if query is None else query, example.bindings,
        ).fetchall()


def replace_once(query, old, new):
    if query.count(old) != 1:
        raise AssertionError(f"Update the deliberate SQL mutation: {old!r}")
    return query.replace(old, new, 1)


class EnglishExampleTests(unittest.TestCase):
    chapter = CHAPTER

    def setUp(self):
        self.example = read_example(self.chapter)

    def assert_result(self, expected):
        actual = execute(self.example)
        reference = reference_result(
            self.example.orders, self.example.lines, self.example.bindings,
        )
        self.assertEqual(reference, expected)
        self.assertEqual(actual, expected)
        for row in actual:
            for value in row[1:]:
                self.assertIs(type(value), int)

    def assert_mutation_detected(self, old, new):
        self.assert_result(self.example.expected)
        query = replace_once(self.example.query, old, new)
        self.assertNotEqual(execute(self.example, query), self.example.expected)

    def test_published_result_matches_sql_and_independent_reference(self):
        self.assertEqual(len(self.example.orders), 8)
        self.assertEqual(len(self.example.lines), 9)
        self.assertEqual(self.example.expected,
                         [("C1", 2, 10, 7000), ("C2", 1, 2, 4000)])
        self.assert_result(self.example.expected)

    def test_join_without_tenant_key_is_detected(self):
        self.assert_mutation_detected("l.tenant_id = o.tenant_id AND ", "")

    def test_missing_customer_scope_is_detected(self):
        self.assert_mutation_detected(
            "AND o.customer_id IN (:customer_a, :customer_b)", "",
        )

    def test_counting_joined_lines_as_orders_is_detected(self):
        self.assert_result(self.example.expected)
        query = replace_once(self.example.query, "o.customer_id,",
                             "o.customer_id, COUNT(*) AS line_count,")
        query = replace_once(query, "COUNT(*) AS pending_orders",
                             "SUM(line_count) AS pending_orders")
        actual = execute(self.example, query)
        self.assertEqual(actual[0], ("C1", 3, 10, 7000))
        self.assertNotEqual(actual, self.example.expected)

    def test_summing_repeated_order_amounts_is_detected(self):
        self.assert_mutation_detected(
            "SUM((l.ordered_qty - l.shipped_qty) * l.unit_price_cents)",
            "SUM(o.total_amount_cents)",
        )

    def test_same_customer_equal_amounts_expose_distinct_under_count(self):
        query = replace_once(
            self.example.query, "SUM(remaining_amount_cents)",
            "SUM(DISTINCT remaining_amount_cents)",
        )
        self.assertEqual(execute(self.example, query), self.example.expected)
        source = next(order for order in self.example.orders
                      if (order.tenant_id, order.order_id) == ("T1", "O102"))
        self.example.orders.append(source._replace(order_id="O108"))
        self.example.lines.append(Line("T1", "O108", 1, 4, 0, 1000))
        self.assert_result([("C1", 3, 14, 11000), ("C2", 1, 2, 4000)])
        self.assertEqual(execute(self.example, query),
                         [("C1", 3, 14, 7000), ("C2", 1, 2, 4000)])

    def test_excluding_left_time_boundary_is_detected(self):
        self.assert_mutation_detected("o.created_at >= :start_utc",
                                      "o.created_at > :start_utc")

    def test_including_right_time_boundary_is_detected(self):
        self.assert_mutation_detected("o.created_at < :end_utc",
                                      "o.created_at <= :end_utc")

    def test_including_cancelled_orders_is_detected(self):
        self.assert_mutation_detected("AND o.status != 'cancelled'", "")

    def test_counting_fully_shipped_orders_is_detected(self):
        self.assert_mutation_detected("WHERE remaining_qty > 0", "")

    def test_no_orders_in_window_returns_no_rows(self):
        self.example.bindings.update(start_utc="2026-08-01T00:00:00Z",
                                     end_utc="2026-08-02T00:00:00Z")
        self.assert_result([])

    def test_missing_column_is_an_error_not_an_empty_result(self):
        query = replace_once(self.example.query, "l.unit_price_cents",
                             "l.missing_unit_price_cents")
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
            execute(self.example, query)


class ChineseExampleTests(EnglishExampleTests):
    chapter = CHAPTER.with_suffix(".zh.md")


class BilingualExampleTests(unittest.TestCase):
    def test_both_editions_have_identical_executable_examples(self):
        self.assertEqual(read_example(CHAPTER),
                         read_example(CHAPTER.with_suffix(".zh.md")))


if __name__ == "__main__":
    unittest.main()
