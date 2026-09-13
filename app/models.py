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
    """Снимок текущих цен/скидок WB по размеру (срез последней загрузки)."""

    __tablename__ = "price_snapshots"
    __table_args__ = (
        UniqueConstraint("article", "size", name="uq_price_snap_article_size"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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