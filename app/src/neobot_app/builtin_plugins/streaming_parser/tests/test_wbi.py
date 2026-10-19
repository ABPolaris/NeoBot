"""WBI 签名（对照 amagi sign/wbi.ts）。"""

from __future__ import annotations

import pytest

from bilibili.wbi import (
    MIXIN_KEY_ENC_TAB,
    WbiSigner,
    build_query,
    encode_wbi,
    extract_key,
    get_mixin_key,
    query_params_of,
)

#: amagi packages/core/test/platforms/bilibili/wbi.test.ts:17-26 使用的密钥对
IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
MIXIN_KEY = "ea1db124af3c7062474693fa704f4ff8"
#: 固定时间戳下的 w_rid；md5 已用独立实现（Windows CNG / Get-FileHash）交叉验证
WTS = 1700000000
W_RID = "c4bc0fb138c1bbd28b3bf00535c0559f"

NAV_BODY = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + IMG_KEY + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + SUB_KEY + ".png",
        },
        "vipStatus": 1,
    },
}


class TestMixinKey:
    def test_table_is_a_permutation_of_0_to_63(self):
        assert len(MIXIN_KEY_ENC_TAB) == 64
        assert sorted(MIXIN_KEY_ENC_TAB) == list(range(64))

    def test_mixin_key_vector(self):
        assert get_mixin_key(IMG_KEY, SUB_KEY) == MIXIN_KEY

    def test_extract_key_takes_filename_stem(self):
        assert extract_key("https://i0.hdslb.com/bfs/wbi/" + IMG_KEY + ".png") == IMG_KEY
        assert extract_key("no-extension") == "no-extension"


class TestEncoding:
    def test_query_string_is_sorted_and_includes_wts(self):
        assert build_query({"type": "1", "oid": "1"}, wts=WTS) == "oid=1&type=1&wts=1700000000"

    def test_signature_vector(self):
        assert encode_wbi({"oid": "1", "type": "1"}, IMG_KEY, SUB_KEY, wts=WTS) == (
            "&wts=1700000000&w_rid=" + W_RID
        )

    def test_filtered_characters_are_stripped_from_values(self):
        assert build_query({"k": "a!b'c(d)e*f"}, wts=WTS) == "k=abcdef&wts=1700000000"

    def test_encode_matches_urllib_quote_for_specials(self):
        # 过滤 !'()* 之后，JS encodeURIComponent 与 Python quote(safe="") 转义集一致
        assert build_query({"k": "a b+c/d?e"}, wts=WTS) == "k=a%20b%2Bc%2Fd%3Fe&wts=1700000000"

    def test_query_params_of_reads_existing_query(self):
        params = query_params_of("https://x/y?oid=1&type=1")
        assert params == {"oid": "1", "type": "1"}


class TestSigner:
    @staticmethod
    def make_fetch(counter: list[int], body=NAV_BODY):
        async def _fetch():
            counter.append(1)
            return body

        return _fetch

    async def test_keys_are_cached_within_ttl(self):
        counter: list[int] = []
        signer = WbiSigner(clock=lambda: 0.0)
        fetch = self.make_fetch(counter)
        for _ in range(3):
            await signer.keys(fetch)
        assert len(counter) == 1

    async def test_keys_are_refetched_after_ttl(self):
        counter: list[int] = []
        now = {"t": 0.0}
        signer = WbiSigner(ttl_seconds=1000, clock=lambda: now["t"])
        fetch = self.make_fetch(counter)
        await signer.keys(fetch)
        now["t"] = 1001.0
        await signer.keys(fetch)
        assert len(counter) == 2

    async def test_invalidate_forces_refetch(self):
        counter: list[int] = []
        signer = WbiSigner(clock=lambda: 0.0)
        fetch = self.make_fetch(counter)
        await signer.keys(fetch)
        signer.invalidate()
        await signer.keys(fetch)
        assert len(counter) == 2

    async def test_sign_appends_wts_and_w_rid_to_existing_query(self):
        signer = WbiSigner(clock=lambda: 0.0)
        signed = await signer.sign(
            "https://api.bilibili.com/x/v2/reply/wbi/main?oid=1&type=1",
            self.make_fetch([]),
        )
        assert signed.startswith("https://api.bilibili.com/x/v2/reply/wbi/main?oid=1&type=1&wts=")
        assert "&w_rid=" in signed

    async def test_missing_wbi_img_raises(self):
        async def _fetch():
            return {"code": 0, "data": {}}

        signer = WbiSigner(clock=lambda: 0.0)
        with pytest.raises(ValueError):
            await signer.keys(_fetch)

