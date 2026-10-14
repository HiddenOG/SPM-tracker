"""
test_normalize.py — Unit tests for string normalization helpers.

normalize_spm_string() strips all whitespace from SPM PO references so that
"S.P.M. - C.N.L. - 3072" and "S.P.M.-C.N.L.-3072" are treated as the same.
"""
import pytest
from supplier_po_parser import normalize_spm_string
from db import normalize_po_number


class TestNormalizeSpmString:
    def test_strips_spaces_around_dashes(self):
        assert normalize_spm_string("S.P.M. - C.N.L. - 3072") == "S.P.M.-C.N.L.-3072"

    def test_already_normalized_unchanged(self):
        assert normalize_spm_string("S.P.M.-C.N.L.-3072") == "S.P.M.-C.N.L.-3072"

    def test_strips_all_internal_spaces(self):
        assert normalize_spm_string("S P M - C N L - 3072") == "SPM-CNL-3072"

    def test_empty_string(self):
        assert normalize_spm_string("") == ""

    def test_none_returns_empty(self):
        assert normalize_spm_string(None) == ""

    def test_tabs_and_newlines_stripped(self):
        assert normalize_spm_string("S.P.M.\t-\nC.N.L.-3072") == "S.P.M.-C.N.L.-3072"


class TestNormalizePoNumber:
    def test_plain_chevron_po(self):
        assert normalize_po_number("0061448980") == "0061448980"

    def test_revision_suffix(self):
        assert normalize_po_number("0060792432-001") == "0060792432-001"

    def test_spaces_around_revision_dash(self):
        assert normalize_po_number("0060792432 - 001") == "0060792432-001"

    def test_revision_zero_padded(self):
        assert normalize_po_number("0060792432-1") == "0060792432-001"

    def test_none_returns_none(self):
        assert normalize_po_number(None) is None

    def test_empty_returns_none(self):
        assert normalize_po_number("") is None

    def test_no_match_returns_none(self):
        assert normalize_po_number("not-a-po-number") is None
