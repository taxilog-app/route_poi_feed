# route_poi_feed

タクシー配車アプリ向けの **POI（まずホテル）データ基盤**。
公開オープンデータ（OpenStreetMap / Wikidata）から収集 → 名前を正規化 → 静的JSONに整形 →
GitHub Pages で無料配信する。アプリ側はこのJSONを読むだけ（端末でスクレイプしない）。

イベント基盤 `route_event_feed` と同じ設計思想（別プロジェクト・サーバー集約・アプリは読むだけ）。

## なぜこの形か

- アプリでスクレイプしない＝端末のCPU/メモリ負荷ゼロ。
- 元データが改修・更新されても、ここを直すだけで全ユーザーに即反映（アプリ更新不要）。
- **個人情報を一切扱わない**（ホテルは公開情報）。サーバー不要・コストほぼ¥0。
- **秘密情報・APIキーを一切使わない**（公開APIの読み取りとPages配信のみ）。

## 名前の正確性（設計の要）

「同じ縮尺でもホテル名が出たり出なかったり」を解消するため、2ソースを合体：

1. **OSM ∪ Wikidata** … 和集合で網羅を上げ、名前はWikidataの正式名を優先。
2. **支店名を復元** … OSMの `branch` から「東横イン 博多駅南」等を合成（支店名は消さない）。
3. **信頼度 `conf`** … 2ソース一致=`verified` / 片方=`single`。
4. **`brandOnly`** … ブランドしか取れないものに印（アプリ側が町丁目で区別）。

最後の数％（日本語名がどの無料ソースにも無い等）は、アプリ側の運転手ワンタップ修正で補う。

## 出力

`out/hotels_jp.json`
```json
{ "schemaVersion": 1, "generatedAt": "...", "region": "fukuoka_central",
  "hotels": [ {"name":"都ホテル博多","lat":..,"lng":..,"type":"hotel","conf":"verified","ref":"..."} ] }
```

## 実行

```
pip install -r requirements.txt
python build_hotels.py   # → out/hotels_jp.json
```

GitHub Actions が週1で実行し GitHub Pages へ配信（`.github/workflows/build.yml`）。

## データ出典・ライセンス

- **OpenStreetMap** contributors — ODbL（表示・継承）。
- **Wikidata** — CC0（パブリックドメイン）。

配信データは上記から生成した事実情報（名称・座標・種別）のみ。
