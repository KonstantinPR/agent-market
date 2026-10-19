# -*- coding: utf-8 -*-
"""Раздел «Автоматизация»: фолдеринг фото, размножение имён, кроп, штрихкоды."""

from fastapi import Form

from app.api._common import *  # noqa: F401,F403
from app.services import barcodes as barcode_service
from app.services import img_cropper as crop_service
from app.services import img_foldering as foldering_service
from app.services import names_multiply as names_service

router = APIRouter()

#: Колонки-кандидаты для артикула и nmID в загружаемом файле.
ARTICLE_COLS = ["Артикул товара", "Артикул", "article", "vendorCode", "Артикул поставщика"]
NM_COLS = ["Article_WB", "nm_id", "nmID", "nm", "Артикул WB"]


def _pick_col(df, candidates):
    low = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand.strip().lower() in low:
            return low[cand.strip().lower()]
    return None


def _parse_file(name: str, data: bytes) -> tuple[list[str], dict]:
    low = (name or "").lower()
    if low.endswith((".xlsx", ".xlsm")):
        df = excel_io.read_excel_bytes(data)
    elif low.endswith(".csv"):
        df = pd.read_csv(BytesIO(data), sep=None, engine="python")
    else:
        df = pd.read_csv(BytesIO(data), sep="\t", header=None, names=["article"])
    df.columns = [str(c).strip() for c in df.columns]
    art_col = _pick_col(df, ARTICLE_COLS) or df.columns[0]
    nm_col = _pick_col(df, NM_COLS)
    if nm_col is None and len(df.columns) >= 2 and art_col == df.columns[0]:
        nm_col = df.columns[1]

    arts: list[str] = []
    nm: dict[str, str] = {}
    for _, row in df.iterrows():
        art = str(row[art_col]).strip() if pd.notna(row[art_col]) else ""
        if not art or art.lower() in ("nan", "none"):
            continue
        arts.append(art)
        if nm_col is not None and pd.notna(row[nm_col]) and str(row[nm_col]).strip():
            nm[art.upper()] = str(row[nm_col]).strip()
    return arts, nm


async def _load_inputs(text: str, upfile) -> tuple[list[str], dict]:
    arts: list[str] = []
    nm: dict[str, str] = {}

    text = (text or "").strip()
    if text:
        if "\t" in text:
            for line in text.splitlines():
                parts = line.split("\t")
                if not parts or not parts[0].strip():
                    continue
                art = parts[0].strip()
                arts.append(art)
                if len(parts) > 1 and parts[1].strip():
                    nm[art.upper()] = parts[1].strip()
        else:
            for tok in text.replace(",", " ").split():
                tok = tok.strip()
                if tok:
                    arts.append(tok)

    if upfile is not None:
        data = await upfile.read()
        if data:
            f_arts, f_nm = _parse_file(upfile.filename or "", data)
            arts.extend(f_arts)
            nm.update(f_nm)
    return arts, nm


def _parse_barcodes_file(name: str, data: bytes) -> list[str]:
    """Файл с баркодами: txt/tsv — построчно (код целиком), csv/xlsx — первый столбец."""
    low = (name or "").lower()
    if low.endswith((".xlsx", ".xlsm")):
        df = excel_io.read_excel_bytes(data)
    elif low.endswith((".csv", ".tsv")):
        df = pd.read_csv(BytesIO(data), sep=None, engine="python", header=None, dtype=str)
    else:
        # txt: каждая строка — цельный код; пробелы и запятые внутри кода сохраняются.
        text = data.decode("utf-8-sig", errors="replace")
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    codes: list[str] = []
    for v in df.iloc[:, 0].tolist():
        s = "" if v is None else str(v).strip()
        if s and s.lower() not in ("nan", "none"):
            codes.append(s)
    return codes


async def _load_barcodes(text: str, upfile) -> list[str]:
    """Коды из textarea и/или файла. textarea: строго построчно — код может
    содержать пробелы, запятые и прочие разделители (GS1/Честный знак)."""
    codes: list[str] = []
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        line = line.strip()
        if line:
            codes.append(line)

    if upfile is not None:
        data = await upfile.read()
        if data:
            codes.extend(_parse_barcodes_file(upfile.filename or "", data))
    return codes


def _db_nm_map(db) -> dict:
    rows = db.execute(select(models.NmArticle.article, models.NmArticle.nm_id)).all()
    return {str(a).strip().upper(): str(n) for a, n in rows if a and n}


def _zip_response(buf, filename: str):
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ шаблон


@router.get("/automation/template")
def automation_template():
    """Excel-шаблон списка артикулов (Артикул + nm_id) с примерами строк."""
    df = pd.DataFrame({
        "Артикул": ["WLP-117116-13", "WLP-117116-14"],
        "nm_id": ["123456789", "123456790"],
    })
    buf = excel_io.df_to_excel_stream(df, sheet_name="Шаблон")
    return StreamingResponse(
        buf,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": 'attachment; filename="template.xlsx"',
            "X-Count": str(len(df)),
        },
    )


# ------------------------------------------------------------------ фолдеринг


@router.post("/automation/foldering/preview")
async def automation_foldering_preview(
    articles: str = Form(""),
    file: UploadFile = File(None),
    marketplace: str = Form("WB"),
    is_replace: str = Form("ONLY_NEW"),
    order: str = Form("ASCENDING"),
    db: Session = Depends(get_db),
):
    arts, nm_extra = await _load_inputs(articles, file)
    if not arts:
        raise HTTPException(status_code=400, detail="Не переданы артикулы")

    mp = (marketplace or "WB").strip().upper()
    nm_map = {} if mp == "OZON" else _db_nm_map(db)
    nm_map.update(nm_extra)

    _entries, report = foldering_service.plan(arts, nm_map, mp, is_replace, order)
    return report


@router.post("/automation/foldering/build")
async def automation_foldering_build(
    articles: str = Form(""),
    file: UploadFile = File(None),
    marketplace: str = Form("WB"),
    is_replace: str = Form("ONLY_NEW"),
    order: str = Form("ASCENDING"),
    leading_nulls: bool = Form(False),
    change_order: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    arts, nm_extra = await _load_inputs(articles, file)
    if not arts:
        raise HTTPException(status_code=400, detail="Не переданы артикулы")

    mp = (marketplace or "WB").strip().upper()
    nm_map = {} if mp == "OZON" else _db_nm_map(db)
    nm_map.update(nm_extra)

    entries, report = foldering_service.plan(arts, nm_map, mp, is_replace, order)
    if not entries:
        raise HTTPException(
            status_code=400,
            detail=f"Нет фото для сборки: не найдено {len(report['missing'])}, без nmID {len(report['no_nm'])}",
        )
    buf = foldering_service.build_zip(entries, leading_nulls=leading_nulls, change_order=change_order)
    return _zip_response(buf, f"foldering_{mp.lower()}.zip")


# ------------------------------------------------------------------ размножение имён


@router.post("/automation/names/multiply")
async def automation_names_multiply(
    articles: str = Form(""),
    file: UploadFile = File(None),
    multiply: int = Form(1),
    start_from: int = Form(1),
    upper: bool = Form(True),
):
    arts, _nm = await _load_inputs(articles, file)
    if not arts:
        raise HTTPException(status_code=400, detail="Не переданы артикулы")
    if multiply < 1:
        raise HTTPException(status_code=400, detail="«Сколько» должно быть ≥ 1")
    items = names_service.names_multiply(arts, multiply, start_from, upper=upper)
    return {"items": items, "count": len(items)}


@router.post("/automation/names/download")
async def automation_names_download(
    articles: str = Form(""),
    file: UploadFile = File(None),
    multiply: int = Form(1),
    start_from: int = Form(1),
    upper: bool = Form(True),
):
    arts, _nm = await _load_inputs(articles, file)
    if not arts:
        raise HTTPException(status_code=400, detail="Не переданы артикулы")
    if multiply < 1:
        raise HTTPException(status_code=400, detail="«Сколько» должно быть ≥ 1")
    items = names_service.names_multiply(arts, multiply, start_from, upper=upper)
    text = names_service.to_text(items)
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="names.txt"'},
    )


# ------------------------------------------------------------------ кроп


@router.post("/automation/crop/build")
async def automation_crop_build(
    files: List[UploadFile] = File(default=[]),
    type_clothes: str = Form("Одежда по пояс"),
    articles: str = Form(""),
):
    items: list[tuple[str, bytes]] = []
    for upf in files or []:
        data = await upf.read()
        if data:
            items.append((upf.filename or "image.jpg", data))

    arts, _nm = await _load_inputs(articles, None)
    index = photos_service.get_index()
    for art in arts:
        ap = index.get(art, allow_prefix=True)
        if not ap:
            continue
        for _num, path in ap.items:
            try:
                items.append((path.name, path.read_bytes()))
            except OSError:
                continue

    if not items:
        raise HTTPException(status_code=400, detail="Нет изображений для обработки")
    buf = crop_service.crop_images(items, type_clothes)
    return _zip_response(buf, "crop.zip")


# ------------------------------------------------------------------ штрихкоды


@router.post("/automation/barcodes/preview")
async def automation_barcodes_preview(
    codes: str = Form(""),
    kind: str = Form("code128"),
    file: UploadFile = File(None),
):
    """Предпросмотр: первые MAX_PREVIEW кодов рендерятся в data-url PNG."""
    if kind not in barcode_service.KINDS:
        raise HTTPException(status_code=400, detail=f"Неизвестный тип штрихкода: {kind}")
    codes_list = await _load_barcodes(codes, file)
    if not codes_list:
        raise HTTPException(status_code=400, detail="Не переданы коды")

    rows = barcode_service.generate_batch(codes_list, kind)
    items = [
        {
            "index": i,
            "code": r["code"],
            "ok": r["ok"],
            "error": r["error"],
            "kind": r["kind"],
            "data_url": barcode_service.to_data_url(r["png"]) if r["png"] else None,
        }
        for i, r in enumerate(rows, 1)
    ]
    return {
        "count": len(items),
        "ok_count": sum(1 for r in rows if r["ok"]),
        "truncated": len(items) > barcode_service.MAX_PREVIEW,
        "items": items,
    }


@router.post("/automation/barcodes/build")
async def automation_barcodes_build(
    codes: str = Form(""),
    kind: str = Form("code128"),
    file: UploadFile = File(None),
):
    """ZIP с PNG-штрихкодами: bar_0001.png, bar_0002.png…"""
    if kind not in barcode_service.KINDS:
        raise HTTPException(status_code=400, detail=f"Неизвестный тип штрихкода: {kind}")
    codes_list = await _load_barcodes(codes, file)
    if not codes_list:
        raise HTTPException(status_code=400, detail="Не переданы коды")
    try:
        buf = barcode_service.build_zip(codes_list, kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _zip_response(buf, f"barcodes_{kind}.zip")


@router.get("/automation/barcodes/catalog")
def automation_barcodes_catalog(
    articles: str = "",
    limit: int = 5000,
    db: Session = Depends(get_db),
):
    """Штрихкоды из каталога: product_sizes.barcode; у товара без размеров —
    fallback на products.barcode."""
    arts = [t.strip().upper() for t in (articles or "").replace(",", " ").split() if t.strip()]
    q = (
        select(
            models.Product.article,
            models.Product.barcode.label("product_barcode"),
            models.ProductSize.id.label("size_id"),
            models.ProductSize.size,
            models.ProductSize.barcode,
        )
        .outerjoin(models.ProductSize, models.ProductSize.article == models.Product.article)
        .order_by(models.Product.article, models.ProductSize.size)
    )
    if arts:
        q = q.where(models.Product.article.in_(arts))

    by_article: dict[str, dict] = {}
    for art, bc_product, size_id, size, bc_size in db.execute(q).all():
        info = by_article.setdefault(art, {"product": bc_product, "sizes": []})
        if size_id is not None:
            info["sizes"].append((size, bc_size))

    items: list[dict] = []
    truncated = False
    for art, info in by_article.items():
        rows = (
            [(size, bc) for size, bc in info["sizes"] if str(bc or "").strip()]
            if info["sizes"]
            else [("", info["product"])]
        )
        for size, bc in rows:
            barcode = str(bc or "").strip()
            if not barcode:
                continue
            if len(items) >= max(int(limit or 0), 1):
                truncated = True
                break
            items.append({"article": art, "size": size or "", "barcode": barcode})
    return {"items": items, "count": len(items), "truncated": truncated}
