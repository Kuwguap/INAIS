from inais.textutil import TELEGRAM_LIMIT, split_message


def test_short_message_single_chunk():
    assert split_message("hello") == ["hello"]


def test_empty_message():
    assert split_message("   ") == []


def test_long_message_splits_under_limit():
    text = "\n\n".join(f"paragraph {i} " + "x" * 500 for i in range(20))
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)


def test_split_prefers_paragraph_boundaries():
    para = "a" * 3000
    text = f"{para}\n\n{para}"
    chunks = split_message(text)
    assert chunks[0] == para
    assert chunks[1] == para


def test_no_content_lost():
    text = " ".join(f"word{i}" for i in range(3000))
    chunks = split_message(text)
    rejoined = " ".join(chunks).split()
    assert rejoined == text.split()


def test_pathological_no_spaces():
    text = "x" * 10000
    chunks = split_message(text)
    assert all(len(c) <= TELEGRAM_LIMIT for c in chunks)
    assert sum(len(c) for c in chunks) == 10000
