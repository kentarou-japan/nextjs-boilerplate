# Codex への引き継ぎ（dododo-siryou で表紙生成から再開）

作成日: 2026-09-28。Claude Code セッションでは Codex 内蔵画像生成が使えず、外部サービス（Higgsfield）の残高も不足したため、画像生成工程を Codex へ移す。**画像はまだ1枚も生成していない。**

## 1. 進行ゲートの状態

| ゲート | 状態 | 記録 |
|---|---|---|
| purpose_confirmed | 済 | 目的：初めて聞く社内関係者が、実先物データでの再検証に進むかを判断できる状態にする |
| scope_and_input_resolved | 済 | `full_deck` / `source_transform` |
| delivery_mode_confirmed | 済 | 出力＝**PDF**、ローカル成果物のみ（公開URLなし） |
| source_analyzed | 済 | `structure_design.md` A-1〜A-5 |
| source_mapping_completed | 済 | 同 A-6（未配置 atom 0件） |
| decision_brief_ready | 済 | 同 B |
| transformation_integrity_passed | 済 | 数値は `docs/07_validation_report.md` と照合済み |
| structure_approved | **済（2026-09-28 ユーザー明示承認）** | 15ページ構成、`structure_design.md` のとおり |
| image_contract_resolved | 未 | `structure_design.md` E の STYLE DNA・固定部品契約・CTA契約を適用（ユーザー固有ルールなし） |
| image_capability_confirmed | 未 | Codex 内蔵画像生成で確認すること |
| cover_generated 以降 | 未 | 次の工程 |

その他の確定事項：制作方式 A（文字入り完成画像）、16:9、投影しながら話す資料、PC、社内限定、運用資金額・成績数値の掲載可。

## 2. Codex に渡すもの

- このファイル
- `trend_bot/docs/presentation/structure_design.md`（承認済み構成設計書。STYLE DNA と固定部品契約を含む）
- 数値の照合元：`trend_bot/docs/07_validation_report.md`

## 3. Codex への依頼文（そのまま貼る）

```text
$dododo-siryou を使って、添付の承認済み構成設計書（structure_design.md）から完成資料を作ってください。
このリポジトリの trend_bot/docs/presentation/CODEX_HANDOFF.md に進行ゲートの状態があります。
目的確認・構成承認・出力形式（PDF、ローカルのみ）は済んでいるので、image_contract_resolved と
image_capability_confirmed を確認したうえで、表紙（P01）1枚だけを生成し、デザイン承認を待ってください。
STYLE DNA・固定部品契約・CTA契約は structure_design.md の E 節を一字一句そのまま使ってください。
画像内の数値は docs/07_validation_report.md と一字ずつ照合してください。
```

## 4. 注意

- dododo の規則どおり、表紙承認前に2枚目以降を作らない。本編標準は P11、CTA標準は P15。
- 画像内の日本語が崩れた場合は、文字を後から重ねず、文字量を減らして再生成する。
- 成績はすべて代替データによる予備検証。「運用実績」「予測」と読める表現を画像に入れない。
