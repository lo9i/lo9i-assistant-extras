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


def test_the_time_zone_picks_the_country_and_its_currency(monkeypatch):
    assert currency.local("America/Argentina/Buenos_Aires") == currency.Currency("ARS", "es_AR")
    assert currency.local("America/Buenos_Aires") == currency.Currency("ARS", "es_AR")
    assert currency.local("Europe/Madrid") == currency.Currency("EUR", "es_ES")
    assert currency.local("UTC") is None and currency.local("Etc/UTC") is None
    monkeypatch.setenv("TZ", ":America/Sao_Paulo")
    assert currency.local() == currency.Currency("BRL", "pt_BR")


def test_the_countrys_currency_is_saved_the_first_time(conn, monkeypatch):
    browser = currency.Currency("GBP", "en_GB")
    monkeypatch.setenv("TZ", "America/Argentina/Buenos_Aires")
    assert currency.ensure(conn, browser) == currency.get(conn) == currency.Currency("ARS", "es_AR")
    monkeypatch.setenv("TZ", "Europe/Madrid")
    assert currency.ensure(conn) == currency.Currency("ARS", "es_AR")


def test_without_a_country_the_browsers_region_is_used(conn):
    assert currency.ensure(conn) is None and currency.get(conn) is None
    assert currency.ensure(conn, currency.Currency("GBP", "en_GB")) == currency.get(conn) == currency.Currency("GBP", "en_GB")


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


def test_formats_list_each_way_a_currency_is_written_once_the_usual_one_first():
    found = currency.formats("CHF")
    examples = [f.example for f in found]
    assert len(examples) == len(set(examples)) >= 3
    assert found[0].locale == currency.locale_for("CHF")
    assert {"de_CH", "fr_CH"} <= {f.locale for f in found}
    assert all("POSIX" not in f.locale and "Computer" not in f.name for f in currency.formats("USD"))


def test_a_format_chosen_before_stays_listed_even_where_the_currency_isnt_used():
    found = currency.formats("ARS", keep="en_US")
    assert [f.locale for f in found] == ["es_AR", "en_US"]
    assert found[1].example == currency.Currency("ARS", "en_US").format(45230.5)
    assert found[0].name == "Spanish (Argentina)"
