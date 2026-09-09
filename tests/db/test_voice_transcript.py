"""Dictated-transcript repair (``VOYN-W0-APP-CONTROL-S6b``).

The cases below are the failure modes the task record names as the known
trap: the namespace token heard as a Russian word, the priority heard as
words or as a Cyrillic homoglyph, the wave heard as a phrase. Each assertion
covers both halves of the contract — the repaired text AND the correction
record that makes the repair visible to the owner.
"""

from __future__ import annotations

import pytest

from command_center.db.voice_transcript import Correction, normalize_transcript


def test_clean_text_is_left_alone_and_reports_nothing():
    result = normalize_transcript("почини кнопку экспорта в настройках")

    assert result.text == "почини кнопку экспорта в настройках"
    assert result.corrections == ()
    assert result.changed is False


def test_whitespace_is_tidied_without_being_reported_as_a_correction():
    result = normalize_transcript("  добавь   задачу  ")

    assert result.text == "добавь задачу"
    assert result.corrections == ()


@pytest.mark.parametrize("heard", ["воин", "война", "войне", "вояж", "voyn", "Voyne"])
def test_the_namespace_token_is_repaired_however_it_was_heard(heard):
    result = normalize_transcript(f"заведи {heard} W0 APP CONTROL")

    assert result.text == "заведи VOYN W0 APP CONTROL"
    assert result.corrections == (Correction(heard=heard, written="VOYN"),)


def test_a_word_that_merely_contains_a_repaired_term_is_not_touched():
    # substring matching is forbidden here for the same reason the backlog
    # parser forbids it: `W0` and `W00` are different values, and so are
    # `воин` and `авиационный`.
    result = normalize_transcript("авиационный регламент")

    assert result.text == "авиационный регламент"
    assert result.corrections == ()


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("приоритет ноль", "P0"),
        ("приоритет P0", "P0"),
        ("пи ноль", "P0"),
        ("пи один", "P1"),
        ("п 2", "P2"),
        ("р0", "P0"),  # Cyrillic homoglyph: renders as P0, is not P0
        ("priority two", "P2"),
    ],
)
def test_priority_is_repaired_from_speech_and_from_homoglyphs(heard, expected):
    result = normalize_transcript(f"срочно, {heard}")

    assert result.text == f"срочно, {expected}"
    assert result.corrections == (Correction(heard=heard, written=expected),)


def test_the_repaired_priority_is_ascii():
    # the parser's priority field is ASCII-only; a Cyrillic `Р0` that looks
    # identical on screen is refused, which is exactly why this repair exists.
    result = normalize_transcript("Р0")

    assert result.text == "P0"
    assert result.text.isascii()


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("волна ноль", "Wave 0"),
        ("волну два", "Wave 2"),
        ("вейв 3", "Wave 3"),
        ("wave one", "Wave 1"),
        ("нулевая волна", "Wave 0"),
        ("вторую волну", "Wave 2"),
    ],
)
def test_wave_phrasing_is_repaired_in_both_word_orders(heard, expected):
    result = normalize_transcript(f"положи в {heard}")

    assert result.text == f"положи в {expected}"
    assert result.corrections == (Correction(heard=heard, written=expected),)


def test_an_unknown_ordinal_before_the_word_wave_is_left_as_dictated():
    result = normalize_transcript("новая волна интеграций")

    assert result.text == "новая волна интеграций"
    assert result.corrections == ()


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("а и си си", "AICC"),
        ("аиси", "AICC"),
        ("эй ай си си", "AICC"),
        ("айос", "AIOS"),
        ("апи", "API"),
        ("пи ар", "PR"),
        ("пи ви эй", "PWA"),
    ],
)
def test_spelled_out_product_terms_are_repaired(heard, expected):
    result = normalize_transcript(f"почини {heard}")

    assert result.text == f"почини {expected}"
    assert result.corrections == (Correction(heard=heard, written=expected),)


def test_a_whole_dictated_request_is_repaired_in_one_pass():
    result = normalize_transcript(
        "заведи задачу воин W0 APP CONTROL, апи не отвечает, волна ноль, пи ноль"
    )

    assert result.text == (
        "заведи задачу VOYN W0 APP CONTROL, API не отвечает, Wave 0, P0"
    )
    assert result.corrections == (
        Correction(heard="волна ноль", written="Wave 0"),
        Correction(heard="пи ноль", written="P0"),
        Correction(heard="воин", written="VOYN"),
        Correction(heard="апи", written="API"),
    )


def test_the_same_repair_is_reported_once_however_often_it_is_heard():
    result = normalize_transcript("воин первый, воин второй")

    assert result.text == "VOYN первый, VOYN второй"
    assert result.corrections == (Correction(heard="воин", written="VOYN"),)


def test_number_words_outside_a_wave_or_priority_stay_words():
    # digits are only spelled out where the grammar has a digit field; a
    # normalizer that rewrote every number word would corrupt the one plain
    # sentence the description field is allowed to be.
    result = normalize_transcript("две кнопки не работают")

    assert result.text == "две кнопки не работают"
    assert result.corrections == ()


def test_repair_never_introduces_grammar_characters():
    # the intake grammar is pipe-delimited and single-line; a normalizer that
    # could inject `|` or a newline would let dictation forge fields.
    result = normalize_transcript("воин ноль | вторая строка")

    assert "\n" not in result.text
    assert result.text.count("|") == 1  # the one the owner actually dictated
