import pytest

from app.phone import normalize_br, same_number, variants


@pytest.mark.parametrize(
    ("raw", "primary", "alternate", "kind"),
    [
        ("5511987654321", "5511987654321", "551187654321", "mobile"),
        ("551187654321", "5511987654321", "551187654321", "mobile"),  # 12-digit mobile → add the 9
        ("11987654321", "5511987654321", "551187654321", "mobile"),  # no country code
        ("1187654321", "5511987654321", "551187654321", "mobile"),  # 10 digits, mobile w/o 9
        ("(11) 98765-4321", "5511987654321", "551187654321", "mobile"),
        ("+55 (11) 98765-4321", "5511987654321", "551187654321", "mobile"),
        ("0055 11 98765-4321", "5511987654321", "551187654321", "mobile"),
        ("011987654321", "5511987654321", "551187654321", "mobile"),  # trunk 0
        ("5549988760799", "5549988760799", "554988760799", "mobile"),
        ("551133334444", "551133334444", None, "landline"),  # 8-digit local starting 2-5
        ("1133334444", "551133334444", None, "landline"),
    ],
)
def test_normalize_br_mobile_and_landline(raw, primary, alternate, kind):
    forms = normalize_br(raw)
    assert forms is not None
    assert forms.primary == primary
    assert forms.alternate == alternate
    assert forms.kind == kind
    assert forms.country == "BR"


def test_normalize_non_br_and_garbage():
    other = normalize_br("+1 415 555 2671 0")  # 12 digits not starting with 55
    assert other is not None and other.country == "other" and other.alternate is None
    # Longer than E.164's 15 digits is not a phone number (and would overflow the
    # 20-character phone columns on PostgreSQL).
    assert normalize_br("5511987654321" + "9" * 60) is None
    assert normalize_br("") is None
    assert normalize_br(None) is None
    assert normalize_br("123") is None
    assert normalize_br("abc") is None


def test_variants_and_same_number():
    assert variants("5511987654321") == ["5511987654321", "551187654321"]
    assert variants("551133334444") == ["551133334444"]
    assert variants("") == []
    assert same_number("5511987654321", "(11) 8765-4321")
    assert not same_number("5511987654321", "5511987654322")
