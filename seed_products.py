"""
OneCard Platform — Product Data Seeder
=======================================
Imports Full Catalogue.xls into the SQLite products table.
Uses classification logic from catalogue_engine.py.
"""
import os
import sys
import pandas as pd
import sqlite3
import re

# ── Classification Maps (from catalogue_engine.py) ──────────────

COUNTRY_MAP = {
    'ksa': 'Saudi Arabia', 'saudi': 'Saudi Arabia', 'sa store': 'Saudi Arabia',
    'uae': 'UAE', 'emirates': 'UAE', 'ae store': 'UAE',
    'egypt': 'Egypt', 'eg store': 'Egypt', 'مصر': 'Egypt',
    'jordan': 'Jordan', 'jo store': 'Jordan',
    'kuwait': 'Kuwait', 'kw store': 'Kuwait',
    'bahrain': 'Bahrain', 'bh store': 'Bahrain',
    'oman': 'Oman', 'om store': 'Oman',
    'qatar': 'Qatar', 'qa store': 'Qatar',
    'iraq': 'Iraq', 'iq store': 'Iraq',
    'lebanon': 'Lebanon', 'lb store': 'Lebanon',
    'usa': 'USA', 'us store': 'USA', 'united states': 'USA',
    'turkey': 'Turkey', 'tr store': 'Turkey', 'türkiye': 'Turkey',
    'uk': 'UK', 'united kingdom': 'UK', 'gb store': 'UK',
    'global': 'Global', 'international': 'Global',
    'france': 'France', 'fr store': 'France',
    'germany': 'Germany', 'de store': 'Germany',
    'spain': 'Spain', 'es store': 'Spain',
    'italy': 'Italy', 'it store': 'Italy',
    'india': 'India', 'in store': 'India',
    'pakistan': 'Pakistan', 'pk store': 'Pakistan',
    'indonesia': 'Indonesia',
    'malaysia': 'Malaysia',
    'morocco': 'Morocco',
    'tunisia': 'Tunisia',
    'nigeria': 'Nigeria',
    'south africa': 'South Africa',
    'brazil': 'Brazil',
    'mexico': 'Mexico',
    'canada': 'Canada',
    'australia': 'Australia',
    'japan': 'Japan',
    'south korea': 'South Korea', 'korea': 'South Korea',
    'china': 'China',
    'philippines': 'Philippines',
    'thailand': 'Thailand',
    'vietnam': 'Vietnam',
    'singapore': 'Singapore',
    'hong kong': 'Hong Kong',
    'taiwan': 'Taiwan',
    'mena': 'MENA',
    'gcc': 'GCC',
    'europe': 'Europe',
    'africa': 'Africa',
    'asia': 'Asia',
    'latin america': 'Latin America',
}

REGION_MAP = {
    'Saudi Arabia': 'GCC', 'UAE': 'GCC', 'Kuwait': 'GCC', 'Bahrain': 'GCC',
    'Oman': 'GCC', 'Qatar': 'GCC',
    'Egypt': 'North Africa', 'Morocco': 'North Africa', 'Tunisia': 'North Africa',
    'Jordan': 'Levant', 'Lebanon': 'Levant', 'Iraq': 'Levant', 'Syria': 'Levant', 'Palestine': 'Levant',
    'USA': 'Americas', 'Canada': 'Americas', 'Brazil': 'Americas', 'Mexico': 'Americas',
    'UK': 'Europe & Africa', 'France': 'Europe & Africa', 'Germany': 'Europe & Africa',
    'Spain': 'Europe & Africa', 'Italy': 'Europe & Africa', 'Turkey': 'Europe & Africa',
    'Nigeria': 'Europe & Africa', 'South Africa': 'Europe & Africa', 'Europe': 'Europe & Africa',
    'India': 'Asia Pacific', 'Pakistan': 'Asia Pacific', 'Indonesia': 'Asia Pacific',
    'Malaysia': 'Asia Pacific', 'Philippines': 'Asia Pacific', 'Thailand': 'Asia Pacific',
    'Vietnam': 'Asia Pacific', 'Singapore': 'Asia Pacific', 'Hong Kong': 'Asia Pacific',
    'Taiwan': 'Asia Pacific', 'Japan': 'Asia Pacific', 'South Korea': 'Asia Pacific',
    'China': 'Asia Pacific', 'Australia': 'Asia Pacific',
    'Global': 'Global', 'MENA': 'MENA', 'GCC': 'GCC',
}

CATEGORY_RULES = [
    (['esim', 'e-sim', 'sim card', 'airalo', 'holafly', 'nomad'], 'eSIM & Connectivity'),
    (['playstation', 'xbox', 'nintendo', 'steam', 'roblox', 'pubg', 'free fire',
      'fortnite', 'riot', 'valorant', 'league of legends', 'garena', 'razer gold',
      'game', 'gaming', 'ea play', 'blizzard', 'epic games', 'mlbb', 'mobile legends',
      'genshin', 'mihoyo', 'supercell', 'clash', 'cod ', 'call of duty'], 'Gaming'),
    (['stc', 'mobily', 'zain', 'ooredoo', 'du ', 'etisalat', 'vodafone', 'orange',
      'telecom', 'recharge', 'top-up', 'topup', 'top up', 'airtime', 'jawwy',
      'virgin mobile', 'lebara', 'friendi', 'salam mobile'], 'Telecom & Recharge'),
    (['netflix', 'spotify', 'youtube', 'shahid', 'iptv', 'disney', 'hbo',
      'apple tv', 'deezer', 'anghami', 'tidal', 'streaming', 'viu'], 'Entertainment & Streaming'),
    (['noon', 'amazon', 'shein', 'jarir', 'namshi', 'extra', 'shopping', 'retail',
      'sivvi', 'ounass', 'max fashion', 'h&m', 'ikea', 'home centre'], 'Shopping & Retail'),
    (['talabat', 'hunger', 'jahez', 'marsool', 'careem food', 'food', 'delivery',
      'toters', 'uber eats', 'mrsool'], 'Food & Delivery'),
    (['careem', 'uber', 'transport', 'taxi', 'ride'], 'Transportation'),
    (['gift', 'voucher', 'card', 'e-gift', 'egift', 'prepaid'], 'Gift Cards & Vouchers'),
    (['microsoft', 'office', 'adobe', 'kaspersky', 'norton', 'software', 'vpn',
      'canva', 'dropbox', 'google workspace'], 'Software & Subscriptions'),
    (['gym', 'fitness', 'health', 'pharmacy', 'wellness'], 'Health & Fitness'),
    (['cinema', 'movie', 'theme park', 'entertainment', 'leisure'], 'Entertainment & Leisure'),
]


def classify_country(name, merchant):
    text = f"{name} {merchant}".lower()
    if 'esim' in text or 'e-sim' in text or 'airalo' in text or 'holafly' in text:
        if any(k in text for k in COUNTRY_MAP):
            for k, v in sorted(COUNTRY_MAP.items(), key=lambda x: -len(x[0])):
                if k in text:
                    return f"eSIM - {v}"
        return "eSIM - Global"
    for keyword, country in sorted(COUNTRY_MAP.items(), key=lambda x: -len(x[0])):
        if keyword in text:
            return country
    return 'Global'


def classify_region(country):
    if country.startswith('eSIM'):
        return 'eSIM'
    return REGION_MAP.get(country, 'Other')


def classify_category(name, merchant):
    text = f"{name} {merchant}".lower()
    for keywords, cat in CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            return cat
    return 'Other'


def seed_products():
    """Read Full Catalogue.xls and import into DB."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Full Catalogue.xls')
    if not os.path.exists(src):
        print(f"  [ERROR] Cannot find: {src}")
        return

    print("  Loading Full Catalogue.xls...")
    df = pd.read_excel(src)
    # Normalize columns
    df.columns = [c.strip() for c in df.columns]

    # Exact-name preferred mapping (matches OneCard system export headers).
    # INTEGRATION NOTE: when the technical team connects the live company system,
    # this mapping is the single place that defines which source fields feed the platform.
    PREFERRED = {
        'product_id':    ['product id'],
        'product_name':  ['product name'],
        'merchant':      ['merchant name'],
        'merchant_id':   ['merchant id'],
        'currency':      ['product currency'],
        'cost':          ['cost price'],
        'default_price': ['default reseller price'],
        'face_value':    ['recommended retail price (resellers currency)'],
    }
    col_map = {}
    cols_lower = {c.lower().strip(): c for c in df.columns}
    for key, names in PREFERRED.items():
        for n in names:
            if n in cols_lower:
                col_map[key] = cols_lower[n]
                break

    # Fallbacks for slightly different header spellings
    for c in df.columns:
        cl = c.lower()
        if 'product_name' not in col_map and 'product' in cl and 'name' in cl: col_map['product_name'] = c
        if 'merchant' not in col_map and 'merchant' in cl and 'name' in cl:    col_map['merchant'] = c
        if 'cost' not in col_map and cl.strip() == 'cost price':               col_map['cost'] = c
        if 'default_price' not in col_map and 'default' in cl and 'price' in cl: col_map['default_price'] = c
        if 'currency' not in col_map and cl.strip() == 'product currency':     col_map['currency'] = c
        if 'face_value' not in col_map and 'recommended retail price' in cl and 'vat' not in cl: col_map['face_value'] = c

    from models import get_db
    conn = get_db()
    conn.execute("DELETE FROM products")  # Clear existing

    count = 0
    for _, row in df.iterrows():
        name = str(row.get(col_map.get('product_name', ''), '')).strip()
        if not name or name == 'nan':
            continue
        merchant = str(row.get(col_map.get('merchant', ''), 'Unknown')).strip()
        if merchant == 'nan': merchant = 'Unknown'
        cost = float(row.get(col_map.get('cost', ''), 0) or 0)
        default_price = float(row.get(col_map.get('default_price', ''), cost) or cost)
        face_value = float(row.get(col_map.get('face_value', ''), default_price) or default_price)
        currency = str(row.get(col_map.get('currency', ''), 'SAR')).strip()
        if currency == 'nan': currency = 'SAR'
        product_id = str(row.get(col_map.get('product_id', ''), '')).strip()
        merchant_id = str(row.get(col_map.get('merchant_id', ''), '')).strip()

        country = classify_country(name, merchant)
        region = classify_region(country)
        category = classify_category(name, merchant)
        oc_margin = round(default_price - cost, 4) if default_price > cost else 0
        oc_margin_pct = round((oc_margin / default_price) * 100, 2) if default_price > 0 else 0

        # Determine popularity (mock sales volume indicator)
        text_lower = name.lower()
        if any(x in text_lower for x in ['stc', 'pubg', 'razer', 'playstation', 'xbox', 'netflix', 'itunes', 'free fire', 'mobily', 'zain', 'shahid', 'noon', 'amazon']):
            popularity = (abs(hash(name)) % 30) + 70 # 70 to 99
        else:
            popularity = (abs(hash(name)) % 50) + 10 # 10 to 60

        is_new = 1 if (count % 35 == 0 or 'esim' in text_lower) else 0

        conn.execute("""INSERT INTO products
                        (product_id, product_name, merchant, merchant_id, category, country, region,
                         currency, cost, default_price, face_value, oc_margin, oc_margin_pct, popularity, is_new)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (product_id, name, merchant, merchant_id, category, country, region,
                      currency, cost, default_price, face_value, oc_margin, oc_margin_pct, popularity, is_new))
        count += 1

    conn.commit()
    conn.close()
    print(f"  [OK] Imported {count} products into database")


if __name__ == '__main__':
    from models import init_db, seed_default_data
    print("=" * 60)
    print("  OneCard Platform — Database Seeder")
    print("=" * 60)
    init_db()
    print("\n[1/3] Creating default users and tier rules...")
    seed_default_data()
    print("\n[2/3] Importing product catalogue...")
    seed_products()
    print("\n[3/3] Done!")
    print("=" * 60)
