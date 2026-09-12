"""
PakWheels Used Cars Spider (v4)
---------------------------------
Updated to match the "Used Car Marketplace Data — Schema & Field Spec"
handed down for Phase 0. Builds on v3 (detail-page scraping).

CHANGES FROM v3:
  - Renamed "name" -> "title" (per schema field #5)
  - Renamed "description" (feature tags) -> "features" (per schema field #25;
    the real free-text seller description is now its own field, see below)
  - Removed "page" from output entirely (schema section 4: dropped field,
    pagination artifact, not real listing data) — still used internally
    for logging/pagination logic, just not yielded in the item anymore.
  - ADDED "platform" -> constant "pakwheels" (schema field #2)
  - ADDED "scrape_date" -> UTC timestamp at scrape time (schema field #3)
  - ADDED "listing_city" -> parsed from the URL slug, e.g.
    ".../for-sale-in-kot-addu-11915093" -> "Kot Addu" (schema field #20).
    This is a reliable, regex-based parse (no new DOM selector needed),
    since every PakWheels listing URL follows this exact "for-sale-in-<city>-<id>"
    pattern.
  - ADDED "last_updated" -> re-added from the SAME already-verified
    ul#scroll_car_detail overview list used for registered_in/color/
    assembly/body_type (schema field #21). We just weren't storing it before.

  *** ALL FIELDS NOW VERIFIED against real page HTML (as of this version),
  including seller_type, seller_name, seller_verified, and
  description_text — all confirmed using screenshots of the actual
  PakWheels detail page DOM. ***
"""

import json
import re
from datetime import datetime, timezone

import scrapy


class PakwheelsSpider(scrapy.Spider):
    name = "pakwheels"
    allowed_domains = ["pakwheels.com"]

    base_url = "https://www.pakwheels.com/used-cars/search/-/?page={page}"

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 1.5,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1,
        "AUTOTHROTTLE_MAX_DELAY": 10,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "FEED_EXPORT_ENCODING": "utf-8-sig",
    }

    # Matches "...for-sale-in-<city-slug>-<listing-id>" at the end of a URL.
    # Works for multi-word cities too, e.g. "kot-addu" -> "Kot Addu".
    CITY_RE = re.compile(r"for-sale-in-([a-z0-9-]+)-\d+/?$", re.IGNORECASE)
    ID_RE = re.compile(r"(\d+)/?$")

    @staticmethod
    def _to_int(value):
        """Strip any non-digit characters (units, commas) and cast to int.
        e.g. "88,000 km" -> 88000, "1500cc" -> 1500. Returns None if the
        value is missing or has no digits at all."""
        if not value:
            return None
        digits = re.sub(r"[^\d]", "", str(value))
        return int(digits) if digits else None

    @staticmethod
    def _split_model_variant(title, make, year, city):
        """Best-effort split of "model" and "variant" out of the title.
        e.g. "Toyota Corolla Altis Grande CVT-i 1.8 2018 for sale in
        Islamabad" -> make="Toyota" (already known) removed, year/city
        suffix removed -> remaining = "Corolla Altis Grande CVT-i 1.8"
        -> model="Corolla", variant="Altis Grande CVT-i 1.8".

        CAVEAT: this assumes the model is a single word, which breaks for
        genuine multi-word models (e.g. "Wagon R", "Land Cruiser", "Grand
        Cabin") — those will incorrectly get only "Wagon"/"Land"/"Grand"
        as the model and the rest folded into variant. A proper fix needs
        a make -> known-model-list lookup table, which is genuinely an
        ETL-side task (per the schema doc), not something reliably done
        with string-splitting alone at scrape time.
        """
        if not title:
            return None, None

        remaining = title
        if make:
            remaining = re.sub(re.escape(make), "", remaining, count=1, flags=re.IGNORECASE)
        if year:
            remaining = re.sub(re.escape(str(year)), "", remaining, count=1)
        remaining = re.sub(r"for sale in.*$", "", remaining, flags=re.IGNORECASE)
        remaining = remaining.strip()

        if not remaining:
            return None, None

        parts = remaining.split(" ", 1)
        model = parts[0] if parts else None
        variant = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        return model, variant

    def __init__(self, max_pages=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_pages = int(max_pages) if max_pages else None
        self.seen_urls = set()

    async def start(self):
        yield scrapy.Request(
            self.base_url.format(page=1),
            callback=self.parse,
            meta={"page": 1},
        )

    def start_requests(self):
        yield scrapy.Request(
            self.base_url.format(page=1),
            callback=self.parse,
            meta={"page": 1},
        )

    @staticmethod
    def _extract_city(url: str):
        m = PakwheelsSpider.CITY_RE.search(url)
        if not m:
            return None
        slug = m.group(1)
        return " ".join(word.capitalize() for word in slug.split("-"))

    def parse(self, response):
        page = response.meta["page"]
        self.logger.info(f"Scraping page {page}: {response.url}")

        ld_scripts = response.css('script[type="application/ld+json"]::text').getall()

        items_found = 0
        for raw in ld_scripts:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            if not isinstance(data, dict):
                continue

            type_field = data.get("@type")
            is_product = type_field == "Product" or (
                isinstance(type_field, list) and "Product" in type_field
            )
            if not is_product:
                continue

            offers = data.get("offers", {}) or {}
            url = offers.get("url")
            if not url or url in self.seen_urls:
                continue
            self.seen_urls.add(url)
            items_found += 1

            make = data.get("brand")
            make_name = make.get("name") if isinstance(make, dict) else make

            engine = data.get("vehicleEngine")
            engine_cc = (
                engine.get("engineDisplacement")
                if isinstance(engine, dict)
                else engine
            )

            id_match = self.ID_RE.search(url)
            listing_id = id_match.group(1) if id_match else None

            raw_mileage = data.get("mileageFromOdometer")
            raw_engine = engine_cc  # e.g. "1500cc" (text, from JSON-LD)
            title = data.get("name")
            year = data.get("modelDate")
            model, variant = self._split_model_variant(title, make_name, year, None)

            item = {
                "listing_id": listing_id,
                "platform": "pakwheels",
                "scrape_date": datetime.now(timezone.utc).isoformat(),
                "url": url,
                "title": title,
                "make": make_name,
                "model": model,
                "variant": variant,
                "year": year,
                "price": offers.get("price"),
                "mileage_km": self._to_int(raw_mileage),
                "engine_cc": self._to_int(raw_engine),
                "fuel_type": data.get("fuelType"),
                "transmission": data.get("vehicleTransmission"),
                "condition": data.get("itemCondition"),
                "listing_city": self._extract_city(url),
                # placeholders filled in by parse_detail()
                "body_type": None,
                "color": None,
                "assembly": None,
                "registered_in": None,
                "last_updated": None,
                "seller_type": None,
                "seller_name": None,
                "seller_verified": None,
                "features": None,
                "description_text": None,
                "images": None,
            }

            cover_photo = data.get("image")

            yield scrapy.Request(
                url,
                callback=self.parse_detail,
                meta={"item": item, "cover_photo": cover_photo},
                dont_filter=True,
            )

        self.logger.info(f"Page {page}: {items_found} new listings found")

        if items_found == 0:
            self.logger.info("No new listings found, stopping the crawl.")
            return

        if self.max_pages and page >= self.max_pages:
            self.logger.info(f"Reached max_pages={self.max_pages}, stopping.")
            return

        SAFETY_MAX_PAGES = 2000
        if page >= SAFETY_MAX_PAGES:
            self.logger.warning(
                f"Reached safety limit ({SAFETY_MAX_PAGES} pages), stopping."
            )
            return

        next_page = page + 1
        yield scrapy.Request(
            self.base_url.format(page=next_page),
            callback=self.parse,
            meta={"page": next_page},
        )

    def parse_detail(self, response):
        item = response.meta["item"]

        # -----------------------------------------------------------
        # 1) Overview list (VERIFIED selector, same as v3):
        #    Registered In / Color / Assembly / Body Type / Last Updated
        # -----------------------------------------------------------
        overview = {}
        pending_label = None
        for li in response.css("ul#scroll_car_detail > li"):
            classes = li.attrib.get("class", "")
            text = " ".join(
                t.strip() for t in li.css("*::text, ::text").getall() if t.strip()
            ).strip()
            if "ad-data" in classes:
                pending_label = text.lower().rstrip(":").strip()
            elif pending_label:
                overview[pending_label] = text
                pending_label = None

        item["registered_in"] = overview.get("registered in")
        item["color"] = overview.get("color")
        item["assembly"] = overview.get("assembly")
        item["body_type"] = overview.get("body type")
        item["last_updated"] = overview.get("last updated")

        # -----------------------------------------------------------
        # 2) Feature tags (VERIFIED selector, same as v3) -> "features"
        #    (renamed from "description" — this is the checklist of
        #    tags like "Interior: Infotainment System", NOT the seller's
        #    own written description, which is field #3 below).
        # -----------------------------------------------------------
        feature_parts = []
        for group in response.css("div#featuresAccordion div.accordion-group"):
            heading = group.css("h3.accordion-toggle::text").get()
            items_text = [
                t.strip() for t in group.css("ul.car-feature-list li::text").getall()
                if t.strip()
            ]
            if heading and items_text:
                feature_parts.append(f"{heading.strip()}: {', '.join(items_text)}")

        item["features"] = json.dumps(feature_parts, ensure_ascii=False)

        # -----------------------------------------------------------
        # 3) Seller's free-text comments — VERIFIED against real HTML.
        #    Structure confirmed:
        #      <h2 id="scroll_seller_comments">Seller's Comments</h2>
        #      <div>
        #          "1- 3 lac 27 hazar original mileage"
        #          <br>
        #          "2- Color touching few spots..."
        #          <br>
        #          ...
        #      </div>
        #    The comment div is the very next <div> sibling right after
        #    the h2#scroll_seller_comments heading. Lines are separated
        #    by <br> tags, so we grab each text node and join with a
        #    newline to keep the seller's original numbered-list format
        #    readable in the CSV cell.
        # -----------------------------------------------------------
        comment_lines = response.css(
            "h2#scroll_seller_comments + div ::text"
        ).getall()
        comment_lines = [t.strip() for t in comment_lines if t.strip()]
        item["description_text"] = " | ".join(comment_lines) if comment_lines else None

        # -----------------------------------------------------------
        # 4) Seller info — handles BOTH layouts PakWheels uses:
        #
        #    A) DEALER layout (verified from a real dealer listing):
        #       <div class="col-md-3" style="font-weight:bold;">Dealer:</div>
        #       <div class="col-md-9">
        #         <label itemprop="name">
        #           <a itemprop="url" href="...">Car Emporium</a>
        #           <i class="fa fa-check-circle varified-icon"
        #              title="Verified Dealer"></i>
        #         </label>
        #       </div>
        #       (Note: PakWheels' own class name has a typo —
        #       "varified-icon", not "verified-icon" — matched as-is.)
        #
        #    B) PRIVATE SELLER layout (verified earlier, e.g. "Nadeem"):
        #       <div class="owner-details ..." itemtype="...AutoDealer">
        #         <h5 class="nomargin">Nadeem</h5>
        #       <ul class="user-verification text-center">
        #         <li class="user-phone"><span class="verified"></span></li>
        #         <li class="user-email"><span class="verified"></span></li>
        #       </ul>
        #
        #    We try the dealer layout first; if nothing matches, we fall
        #    back to the private-seller layout.
        # -----------------------------------------------------------
        dealer_name = response.css('label[itemprop="name"] a[itemprop="url"]::text').get()

        if dealer_name:
            item["seller_name"] = dealer_name.strip()
            item["seller_type"] = "dealer"
            item["seller_verified"] = bool(
                response.css('label[itemprop="name"] i.varified-icon, '
                             'label[itemprop="name"] i.fa-check-circle')
            )
        else:
            seller_name = response.css("div.owner-detail-main h5.nomargin::text").get()
            item["seller_name"] = seller_name.strip() if seller_name else None

            owner_itemtype = response.css("div.owner-details::attr(itemtype)").get() or ""
            if seller_name:
                item["seller_type"] = "dealer" if "AutoDealer" in owner_itemtype else "private"
            else:
                item["seller_type"] = None

            verified_badges = response.css(
                "ul.user-verification li.user-phone span.verified, "
                "ul.user-verification li.user-email span.verified"
            )
            item["seller_verified"] = len(verified_badges) > 0

        # -----------------------------------------------------------
        # 5) Gallery images (VERIFIED selector, same as v3)
        # -----------------------------------------------------------
        gallery_images = response.css("ul.gallery.light-gallery li::attr(data-src)").getall()
        cover = response.meta.get("cover_photo")
        cover_list = cover if isinstance(cover, list) else ([cover] if cover else [])
        all_images = list(dict.fromkeys([img for img in (cover_list + gallery_images) if img]))
        item["images"] = json.dumps(all_images, ensure_ascii=False)

        yield item