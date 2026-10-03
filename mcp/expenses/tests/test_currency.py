import pytest

from expenses import currency, db
from expenses.service import ValidationError


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    yield conn
    conn.close()


def test_the_browsers_region_picks_the_currency():
    assert currency.guess("es-AR,es;q=0.9") == currency.Currency("ARS", "es_AR")
    assert currency.guess("en, pt-BR;q=0.5") == currency.Currency("BRL", "pt_BR")
    assert currency.guess("en") is None and currency.guess("") is None


def test_a_currency_alone_gets_the_locale_of_its_country():
    assert [currency.locale_for(c) for c in ("ARS", "USD", "JPY", "GBP")] == ["es_AR", "en_US", "ja_JP", "en_GB"]


def test_one_currency_is_kept_and_changing_it_keeps_the_locale(conn):
    assert currency.get(conn) is None
    assert currency.set(conn, "ars") == currency.Currency("ARS", "es_AR")
    assert currency.set(conn, "USD") == currency.Currency("USD", "es_AR")
    assert currency.set(conn, "USD", "en-US") == currency.get(conn) == currency.Currency("USD", "en_US")
    with pytest.raises(ValidationError):
        currency.set(conn, "PESOS")


def test_amounts_are_written_and_read_in_the_locale():
    ars, usd = currency.Currency("ARS", "es_AR"), currency.Currency("USD", "en_US")
    assert (ars.format(45230.5), ars.plain(45230.5), ars.parse("45.230,50")) == ("$45.230,50", "45230,5", 45230.5)
    assert (usd.format(45230.5), usd.plain(1160000.0), usd.parse("45,230.50")) == ("$45,230.50", "1160000", 45230.5)
    with pytest.raises(ValueError, match="write it like"):
        ars.parse("45230.50")
