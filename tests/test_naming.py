from yt2drive.naming import safe_filename, target_name, title_key


def test_strips_illegal_characters():
    assert safe_filename('A/B:C?D*E"F<G>H|I') == "A_B_C_D_E_F_G_H_I"


def test_collapses_whitespace_and_trailing_dots():
    assert safe_filename("  hello   world . ") == "hello world"


def test_windows_reserved_names_are_escaped():
    assert safe_filename("CON") == "_CON"
    assert safe_filename("com1") == "_com1"


def test_empty_title_falls_back():
    assert safe_filename("", fallback="abc123") == "abc123"
    assert safe_filename("///") != ""


def test_long_titles_are_truncated():
    assert len(safe_filename("x" * 400)) <= 120


def test_target_name_embeds_id():
    assert target_name("My Song", "dQw4w9WgXcQ", ".m4a") == "My Song [dQw4w9WgXcQ].m4a"
    assert target_name("My Song", "dQw4w9WgXcQ", "m4a") == "My Song [dQw4w9WgXcQ].m4a"


def test_title_key_ignores_upload_decoration():
    a = title_key("Artist - Song (Official Music Video)")
    b = title_key("Artist - Song [HD] (Lyrics)")
    c = title_key("artist  -  song")
    assert a == b == c


def test_title_key_strips_featured_artists():
    assert title_key("Song feat. Someone Else") == title_key("Song")
    assert title_key("Song ft Someone") == title_key("Song")


def test_title_key_strips_leading_channel_name():
    assert title_key("Some Channel - Track One", uploader="Some Channel") == title_key("Track One")


def test_title_key_distinguishes_real_differences():
    assert title_key("Song One") != title_key("Song Two")


def test_title_key_handles_unicode():
    assert title_key("Café Sessión") == title_key("cafe session")
