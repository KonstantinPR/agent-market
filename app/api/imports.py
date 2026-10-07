# -*- coding: utf-8 -*-
"""Загрузки файлов: /api/import/*."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _MAX_CARDS_UPLOAD,
)

router = APIRouter()



@router.post("/import/products")
async def import_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул поставщика": "article",
        "Артикул": "article",
        "article": "article",
        "Себестоимость": "net_cost",
        "Себестоимость БАЗА": "net_cost",
        "net_cost": "net_cost",
        "Наименование": "name",
        "name": "name",
    })
    n = sync_service.upsert_products(db, df)
    return {"imported": n, "total": len(df), "filename": file.filename}


@router.post("/import/net-cost")
async def import_net_cost(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Обновляет себестоимость (net_cost) товаров из файла с колонками article/net_cost."""
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул": "article",
        "article": "article",
        "Себестоимость": "net_cost",
        "net_cost": "net_cost",
    })
    if "article" not in df.columns or "net_cost" not in df.columns:
        raise HTTPException(status_code=400, detail="В файле нет колонок article и net_cost")
    n = sync_service.upsert_products(db, df)
    filled = common_service.count_products_with_cost(db)
    return {"imported": n, "total": len(df), "with_cost_total": filled, "filename": file.filename}


@router.post("/import/custom-stock")
async def import_custom_stock(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул": "article",
        "Количество": "quantity",
        "Кол-во": "quantity",
        "Закупочная цена": "net_cost",
        "Закуп. цена": "net_cost",
        "Себестоимость": "net_cost",
    })
    n = sync_service.upsert_custom_stock(db, df)
    return {"imported": n, "filename": file.filename}


@router.post("/import/cards")
async def import_cards(
    marketplace: str = "wb",
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Загрузка карточек товара маркетплейса из Excel.

    Принимает 1 файл, несколько файлов или zip-архив с Excel-файлами (*.xlsx, *.xlsm).
    Пишет в marketplace_cards и обновляет общий каталог (products + nm_articles).
    """
    if marketplace not in ("wb", "ozon"):
        raise HTTPException(status_code=400, detail="marketplace должен быть wb или ozon")
    api_code = "wb" if marketplace == "wb" else "ozon"
    frames = []
    total_rows = 0
    errors = []
    fileinfo = []
    accum = 0
    for upf in files:
        data = await upf.read()
        accum += len(data)
        if accum > _MAX_CARDS_UPLOAD:
            raise HTTPException(status_code=413, detail="Слишком большой объём файлов (> 100 МБ)")
        name = upf.filename or "file"
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(BytesIO(data)) as zf:
                    sub = 0
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if not info.filename.lower().endswith((".xlsx", ".xlsm")):
                            continue
                        try:
                            d = excel_io.read_excel_bytes(zf.read(info))
                            frames.append(d)
                            sub += len(d)
                        except Exception as e:  # noqa: BLE001
                            errors.append(f"{name}/{info.filename}: {e}")
                fileinfo.append({"name": name, "rows": sub, "ok": True})
                total_rows += sub
            except zipfile.BadZipFile as e:
                errors.append(f"{name}: не является zip-архивом ({e})")
                fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
            continue
        if not name.lower().endswith((".xlsx", ".xlsm")):
            errors.append(f"{name}: пропущен (не Excel)")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": "не Excel"})
            continue
        try:
            d = excel_io.read_excel_bytes(data)
            frames.append(d)
            total_rows += len(d)
            fileinfo.append({"name": name, "rows": len(d), "ok": True})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
    if not frames:
        raise HTTPException(status_code=400,
                            detail="Нет данных для импорта: " + ("; ".join(errors) or "нет файлов"))
    df = pd.concat(frames, ignore_index=True)
    mdf = sync_service.normalize_marketplace_cards(df)
    n_sk = sync_service.upsert_marketplace_cards(db, mdf, marketplace) if mdf is not None else 0
    n_pr, n_nm = sync_service.refresh_products_from_cards(db, marketplace)
    sync_service.record_api_pull(db, api_code, "cards_excel", int(total_rows), int(n_sk),
                                 f"файлы: {len(files)}")
    return {
        "imported": n_sk,
        "skus": n_sk,
        "products": n_pr,
        "nm_articles": n_nm,
        "total": int(total_rows),
        "files": fileinfo,
        "errors": errors,
    }
