# -*- coding: utf-8 -*-
"""Раздел «Автоматизация»: фолдеринг фото, размножение имён, кроп."""

from fastapi import Form

from app.api._common import *  # noqa: F401,F403
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
):
    arts, _nm = await _load_inputs(articles, file)
    if not arts:
        raise HTTPException(status_code=400, detail="Не переданы артикулы")
    if multiply < 1:
        raise HTTPException(status_code=400, detail="«Сколько» должно быть ≥ 1")
    items = names_service.names_multiply(arts, multiply, start_from)
    return {"items": items, "count": len(items)}


@router.post("/automation/names/download")
async def automation_names_download(
    articles: str = Form(""),
    file: UploadFile = File(None),
    multiply: int = Form(1),
    start_from: int = Form(1),
):
    arts, _nm = await _load_inputs(articles, file)
    if not arts:
        raise HTTPException(status_code=400, detail="Не переданы артикулы")
    if multiply < 1:
        raise HTTPException(status_code=400, detail="«Сколько» должно быть ≥ 1")
    items = names_service.names_multiply(arts, multiply, start_from)
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
