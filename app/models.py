from datetime import date as date_type, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Marketplace(Base):
    __tablename__ = "marketplaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    sales: Mapped[list["Sale"]] = relationship(back_populates="marketplace")
    stocks: Mapped[list["Stock"]] = relationship(back_populates="marketplace")


class Product(Base):
    __tablename__ = "products"

    article: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    barcode: Mapped[str] = mapped_column(String(100), default="")
    brand: Mapped[str] = mapped_column(String(200), default="")
    net_cost: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    replenishable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_id", "date", "article", "source",
            name="uq_sales_market_date_article_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace_id: Mapped[int] = mapped_column(ForeignKey("marketplaces.id"), nullable=False)
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    article: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="v5", server_default="v5")

    quantity: Mapped[int] = mapped_column(Integer, default=0)
    returns_qty: Mapped[int] = mapped_column(Integer, default=0)

    revenue: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    commission: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    logistics: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    storage: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    services: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    income: Mapped[float] = mapped_column(Numeric(14, 2), default=0)

    marketplace: Mapped[Marketplace] = relationship(back_populates="sales")


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = (
        UniqueConstraint(
            "marketplace_id", "date", "article", "warehouse", "chrt_id",
            name="uq_stocks_market_date_article_wh_chrt",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace_id: Mapped[int] = mapped_column(ForeignKey("marketplaces.id"), nullable=False)
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    article: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    warehouse: Mapped[str] = mapped_column(String(200), default="Все")
    chrt_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    size: Mapped[str] = mapped_column(String(50), default="")
    barcode: Mapped[str] = mapped_column(String(100), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    quantity_full: Mapped[int] = mapped_column(Integer, default=0)
    in_way: Mapped[int] = mapped_column(Integer, default=0)

    marketplace: Mapped[Marketplace] = relationship(back_populates="stocks")


class CustomStock(Base):
    __tablename__ = "custom_stock"

    article: Mapped[str] = mapped_column(String(100), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    net_cost: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class FunnelMetric(Base):
    __tablename__ = "funnel_metric"
    __table_args__ = (
        UniqueConstraint("date_from", "date_to", "article", name="uq_funnel_period_article"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date_from: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    date_to: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    nm_id: Mapped[str] = mapped_column(String(40), default="")
    article: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    opens: Mapped[int] = mapped_column(Integer, default=0)
    adds: Mapped[int] = mapped_column(Integer, default=0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)
    buyouts: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    revenue: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    buyout_sum: Mapped[float] = mapped_column(Numeric(14, 2), default=0)


class MarketplaceCard(Base):
    """Карточки товара маркетплейса «как есть»: одна строка — размер/SKU.

    Для WB — штатная выгрузка ЛК (chrt_id, артикул продавца, артикул WB,
    размер, баркод, объём, состав). В будущем то же для Ozon и др.
    (поле marketplace_id).
    """

    __tablename__ = "marketplace_cards"
    __table_args__ = (
        UniqueConstraint("marketplace_id", "chrt_id", name="uq_mp_cards_marketplace_chrt"),
        Index("ix_mp_cards_barcode", "barcode"),
        Index("ix_mp_cards_vendor", "vendor_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    marketplace_id: Mapped[int] = mapped_column(ForeignKey("marketplaces.id"), nullable=False)
    chrt_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    nm_id: Mapped[str] = mapped_column(String(40), default="")
    vendor_code: Mapped[str] = mapped_column(String(200), default="")
    brand: Mapped[str] = mapped_column(String(200), default="")
    subject: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[str] = mapped_column(String(50), default="")
    barcode: Mapped[str] = mapped_column(String(200), default="")
    volume_l: Mapped[float] = mapped_column(Numeric(10, 3), default=0)
    composition: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str] = mapped_column(String(500), default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class NmArticle(Base):
    """Карта nmID Wildberries -> артикул продавца (из Карточек товара)."""

    __tablename__ = "nm_articles"

    nm_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    article: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class PriceChange(Base):
    """Журнал решений автопилота цен WB (applied и skipped с причинами)."""

    __tablename__ = "price_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    nm_id: Mapped[str] = mapped_column(String(40), default="")
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    before_discount: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    after_discount: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    action: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(30), default="")
    reason: Mapped[str] = mapped_column(String(500), default="")


class ApiPull(Base):
    """Лог последних успешных загрузок из API маркетплейсов (по api+kind)."""

    __tablename__ = "api_pulls"
    __table_args__ = (
        UniqueConstraint("api", "kind", name="uq_api_pull_api_kind"),
    )

    api: Mapped[str] = mapped_column(String(20), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    last_success_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    rows: Mapped[int] = mapped_column(Integer, default=0)
    db_rows: Mapped[int] = mapped_column(Integer, default=0)
    window: Mapped[str] = mapped_column(String(50), default="")


class RefreshRun(Base):
    """История массовых обновлений данных (кнопка «Обновить WB/Ozon»)."""

    __tablename__ = "refresh_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    results: Mapped[str] = mapped_column(Text, default="[]")


class PriceSnapshot(Base):
    """Снимок текущих цен/скидок WB|Ozon по размеру (срез последней загрузки)."""

    __tablename__ = "price_snapshots"
    __table_args__ = (
        UniqueConstraint("article", "size", name="uq_price_snap_article_size"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(10), default="wb", index=True)
    article: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    nm_id: Mapped[str] = mapped_column(String(40), default="")
    size: Mapped[str] = mapped_column(String(50), default="")
    price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    discounted_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    discount: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class StorageCost(Base):
    """Стоимость хранения WB по товару (срез последней загрузки)."""

    __tablename__ = "storage_costs"
    __table_args__ = (
        UniqueConstraint("nm_id", name="uq_storage_cost_nm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nm_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    article: Mapped[str] = mapped_column(String(100), default="", index=True)
    barcodes_count: Mapped[int] = mapped_column(Integer, default=0)
    volume: Mapped[float] = mapped_column(Numeric(10, 3), default=0)
    storage_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    warehouse_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class Counterparty(Base):
    """Контрагент учётной системы «Наш склад»: поставщик/покупатель/маркетплейс.

    WB и Ozon заводятся сразу как контрагенты типа `marketplace` — отгрузка
    в их сторону = передача товара на склад маркетплейса.
    """

    __tablename__ = "counterparties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    inn: Mapped[str] = mapped_column(String(20), default="")
    ctype: Mapped[str] = mapped_column(String(20), default="other")  # supplier|buyer|marketplace|carrier|other
    phone: Mapped[str] = mapped_column(String(50), default="")
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class WarehouseDoc(Base):
    """Документ складского учёта: Приход (receipt) или Отгрузка (shipment).

    Остатки считаются как ∑приход − ∑отгрузка. Документ — шапка (дата, №,
    контрагент) + строки items. Excel-файл = пачка документов: строки
    группируются в документы по (дата, №, контрагент).
    """

    __tablename__ = "warehouse_docs"
    __table_args__ = (
        UniqueConstraint(
            "doc_type", "doc_num", "doc_date", "counterparty_id",
            name="uq_wh_doc_type_num_date_cp",
        ),
        Index("ix_wh_docs_type_date", "doc_type", "doc_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # receipt | shipment
    doc_num: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    doc_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparties.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(1000), default="")
    source: Mapped[str] = mapped_column(String(20), default="excel")  # excel | ui | disk
    total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    items: Mapped[list["WarehouseDocItem"]] = relationship(
        back_populates="doc", cascade="all, delete-orphan"
    )


class WarehouseDocItem(Base):
    """Строка документа «Наш склад»: артикул, наименование, кол-во, цена."""

    __tablename__ = "warehouse_doc_items"
    __table_args__ = (Index("ix_wh_items_article", "article"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("warehouse_docs.id", ondelete="CASCADE"), nullable=False)
    article: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)

    doc: Mapped[WarehouseDoc] = relationship(back_populates="items")


class WbDetailRow(Base):
    """Строки «Детализации продаж» WB (финансовый отчёт) из файлов ЛК или finance-API.

    Ключ op_key — идентификатор операции: srid (если есть), иначе report_id:rrd_id,
    иначе rrd_id (для API), иначе номер строки. Уникальный ключ позволяет
    идемпотентно перезаписывать строки повторными загрузками: zip-файлы и
    finance-API пишут в один и тот же ключ и не конфликтуют.
    """

    __tablename__ = "wb_detail_rows"
    __table_args__ = (
        UniqueConstraint("op_key", name="uq_wb_detail_op_key"),
        Index("ix_wb_detail_dates", "sale_dt"),
        Index("ix_wb_detail_article", "article"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    op_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # excel | api
    report_id: Mapped[str] = mapped_column(String(40), default="")
    rrd_id: Mapped[str] = mapped_column(String(40), default="")
    gi_id: Mapped[str] = mapped_column(String(40), default="")
    nm_id: Mapped[str] = mapped_column(String(40), default="")
    article: Mapped[str] = mapped_column(String(100), default="")
    brand: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    tech_size: Mapped[str] = mapped_column(String(50), default="")
    sku: Mapped[str] = mapped_column(String(200), default="")
    doc_type_name: Mapped[str] = mapped_column(String(60), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    retail_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    retail_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    commission_percent: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    office_name: Mapped[str] = mapped_column(String(200), default="")
    sale_dt: Mapped[date_type] = mapped_column(Date, nullable=True)
    order_dt: Mapped[date_type] = mapped_column(Date, nullable=True)
    ppvz_sales_commission: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    for_pay: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    delivery_service: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    delivery_count: Mapped[int] = mapped_column(Integer, default=0)  # «Количество доставок»
    return_delivery_count: Mapped[int] = mapped_column(Integer, default=0)  # «Количество возврата»
    paid_storage: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    pvz_compensation: Mapped[float] = mapped_column(Numeric(14, 2), default=0)  # «Возмещение за выдачу и возврат товаров на ПВЗ»
    payment_services: Mapped[float] = mapped_column(Numeric(14, 2), default=0)  # «Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов»
    penalty: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    deduction: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    additional_payment: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    rebill_logistic_cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    srid: Mapped[str] = mapped_column(String(120), default="")
    order_uid: Mapped[str] = mapped_column(String(120), default="")
    imported_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )