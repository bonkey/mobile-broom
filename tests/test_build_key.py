from mobile_broom.finders._util import build_key, file_url_path, version_tuple, when


def test_build_key_orders_apple_builds():
    builds = ["24A5408d", "23F77", "24A5370g", "23F84", "24A5390f", "22B83"]
    assert sorted(builds, key=build_key) == [
        "22B83",
        "23F77",
        "23F84",
        "24A5370g",
        "24A5390f",
        "24A5408d",
    ]


def test_build_key_tolerates_garbage():
    assert build_key("") < build_key("23F77")
    assert build_key("weird") == (0, "", 0, "weird")


def test_version_tuple():
    assert version_tuple("26.5.2") > version_tuple("26.5")
    assert version_tuple("27.0") > version_tuple("26.5.2")


def test_when_never():
    assert when(None) == "never"


def test_file_url_path():
    assert (
        file_url_path({"relative": "file:///System/Library/x.asset/AssetData/"})
        == "/System/Library/x.asset/AssetData/"
    )
    assert file_url_path("/plain/path") == "/plain/path"
    assert file_url_path(None) is None
