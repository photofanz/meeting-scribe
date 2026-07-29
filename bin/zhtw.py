"""
Simplified -> Traditional (Taiwan) conversion tuned for business meetings.

Why not OpenCC's `s2twp`?
  Whisper already transcribes *Taiwanese* vocabulary — it writes 數位轉型,
  應用程式, 估價單, 權限 — just in simplified glyphs. `s2twp` then applies a
  mainland->Taiwan *phrase* table on top and mistranslates already-correct
  terms: 權限 -> 許可權, 資訊安全 -> 資訊保安, 質量 -> 品質 (wrong when the
  speaker meant physics mass).

So: character-level `s2tw` + a conservative, hand-checked phrase table for the
cases where Whisper genuinely normalizes to mainland vocabulary.
"""
from __future__ import annotations

import re
from opencc import OpenCC

_cc = OpenCC("s2tw")

# Only unambiguous mainland -> Taiwan business/tech terms.
# Ordered longest-first at build time to avoid partial-overlap bugs.
_FIXES: dict[str, str] = {
    # 記帳 / 財務
    "對賬": "對帳", "賬號": "帳號", "賬戶": "帳戶", "賬單": "帳單",
    "賬務": "帳務", "對賬單": "對帳單", "記賬": "記帳", "賬期": "帳期",
    # IT 基礎
    "軟件": "軟體", "硬件": "硬體", "插件": "外掛", "控件": "元件",
    "網絡": "網路", "服務器": "伺服器", "數據庫": "資料庫", "數據": "資料",
    "內存": "記憶體", "硬盤": "硬碟", "文件夾": "資料夾", "打印": "列印",
    "屏幕": "螢幕", "缺省": "預設", "默認": "預設", "接口": "介面",
    "端口": "連接埠", "帶寬": "頻寬", "字節": "位元組", "代碼": "程式碼",
    "程序員": "工程師", "菜單": "選單", "登錄": "登入", "註銷": "登出",
    "上傳下載": "上傳下載", "雲計算": "雲端運算", "計算機": "電腦",
    "移動端": "行動裝置", "移動應用": "行動應用", "小程序": "小程式",
    # 資訊 / 溝通
    "信息": "資訊", "信息化": "資訊化", "視頻會議": "視訊會議",
    "視頻通話": "視訊通話", "音頻": "音訊", "短信": "簡訊",
    "郵件地址": "電子郵件地址", "網民": "網友",
    # 商務
    "項目": "專案", "項目經理": "專案經理", "渠道": "通路",
    "營銷": "行銷", "市場營銷": "行銷", "調研": "調查",
    "供應鏈金融": "供應鏈金融", "反饋": "回饋", "調用": "呼叫",
    "激活": "啟用", "優化": "優化", "落地": "落地",
    "質檢": "品管", "庫存週轉": "庫存週轉", "物流": "物流",
    "人力資源部": "人資部", "績效考核": "績效考核",
}
_PATTERN = re.compile("|".join(
    re.escape(k) for k in sorted(_FIXES, key=len, reverse=True)))


def to_zhtw(text: str) -> str:
    if not text:
        return text
    out = _cc.convert(text)
    return _PATTERN.sub(lambda m: _FIXES[m.group(0)], out)


if __name__ == "__main__":
    import sys
    sys.stdout.write(to_zhtw(sys.stdin.read()))
