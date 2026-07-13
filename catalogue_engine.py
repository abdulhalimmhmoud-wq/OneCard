"""
Catalogue Management System v2 - Engine
========================================
Reads the Full Catalogue, classifies products by country/region and category,
computes tier pricing based on % of OneCard's margin, and generates:
  1. Organized_Catalogue_Master.xlsx (structured Excel workbook)
  2. catalogue_data.json (JSON data for the web dashboard)

NO customer data is included - this tool is for pricing NEW customers.
"""

import pandas as pd
import numpy as np
import json
import os
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# SECTION 1: CONFIGURATION
# =============================================================================

# --- Country classification rules ---

CURRENCY_TO_COUNTRY = {
    'SAR': 'Saudi Arabia',
    'AED': 'UAE',
    'EGP': 'Egypt',
    'KWD': 'Kuwait',
    'JOD': 'Jordan',
    'QAR': 'Qatar',
    'LBP1': 'Lebanon',
    'EUR': 'Europe',
    'USD': 'Global',
}

COUNTRY_KEYWORDS = {
    'Saudi Arabia': ['ksa', 'saudi', 'sawa', 'stc', 'mobily', 'zain - saudi', 'lebara', 'red bull mobile', 'quick net', 'friendi'],
    'UAE': ['uae', 'emirates', 'etisalat', 'du telecom'],
    'Kuwait': ['kuwait'],
    'Bahrain': ['bahrain'],
    'Qatar': ['qatar'],
    'Oman': ['oman'],
    'Egypt': ['egypt', 'egy', 'vodafone egypt', 'orange egypt', 'etisalat egypt', 'we egypt'],
    'Jordan': ['jordan'],
    'Iraq': ['iraq'],
    'Lebanon': ['lebanon'],
    'France': ['france', 'french'],
    'UK': ['uk store', 'united kingdom'],
    'USA': ['us store', 'usa', 'us accounts'],
    'South Africa': ['south africa'],
    'Canada': ['canada'],
    'Germany': ['german'],
}

CATEGORY_RULES = {
    'Gaming': [
        'playstation', 'xbox', 'steam', 'roblox', 'pubg', 'fortnite', 'free fire',
        'mobile legends', 'razer gold', 'riot', 'valorant', 'league of legends',
        'ea sports', 'fc mobile', 'apex legends', 'minecraft', 'nintendo',
        'crossfire', 'lords mobile', 'ludo club', 'yalla ludo', 'parchis',
        'silkroad', 'runescape', 'tibia', 'conquer online', 'webzen',
        'nexon', 'ncoin', 'blizzard', 'heroes evolved', 'gamepower',
        'gamestop', 'viking rise', 'nida al harb', 'jawaker', 'teen patti',
        'merge kingdom', 'wolfteam', 'zaman almoharbeen', 'soho 101',
        'neon games', 'ladypopular', 'soulchill', 'sweater', 'tarbi3ah',
        'lamsa', 'baloot', 'r.o.h.a.n',
    ],
    'Telecom & Recharge': [
        'sawa', 'mobily', 'zain', 'stc', 'lebara', 'quick net', 'friendi',
        'red bull mobile', 'redbull', 'go telecom', 'vodafone', 'orange',
        'etisalat', 'we egypt', 'umniah', 'omantel', 'renna', 'mobecall',
        'monthly installments', 'utility bills',
    ],
    'Gift Cards & Vouchers': [
        'amazon', 'itunes', 'apple gift', 'google play', 'ebay', 'noon giftcard',
        'shukran', 'onecard', 'paysafecard', 'karma koin', 'trip gift',
        'toursgift',
    ],
    'Food & Delivery': [
        'hungerstation', 'talabat', 'jahez', 'mrsool', 'keeta', 'the chefz',
        'maestro pizza', 'tako hut', 'sizzler', 'cold stone', 'saadeddin',
        'patchi', 'starbucks', 'panda',
    ],
    'Entertainment & Streaming': [
        'netflix', 'spotify', 'shahid', 'mbc shahid', 'starzplay', 'hulu',
        'tod', 'spacetoon', 'shofha', 'noor play', 'osn', 'anghami',
        'twitch', 'yalla live', 'imo', 'viber', 'tiktok',
    ],
    'Shopping & Retail': [
        'noon', 'shein', 'namshi', 'h&m', 'lacoste', 'hugo boss',
        'polo ralph', 'tory burch', 'under armour', 'skechers', 'redtag',
        'steve madden', 'naturalizer', 'macqueen', 'styli', 'ubuy',
        'vogacloset', 'kaafmeem', 'centrepoint', 'max giftcard',
        'lifestyle giftcard', 'splash giftcard', 'babyshop', 'home box',
        'home centre', 'shoe express', 'shoemart', 'lulu', 'extra',
        'saco', 'jarir', 'tamimi', 'whites', 'joyalukkas', 'kalyan',
        "l'occitane", 'rituals', 'magrabi', 'golden scent', 'haddad',
        'mall of the emirates', 'majid al futtaim', 'highpoint',
    ],
    'eSIM & Connectivity': [
        'esim', 'e-sim',
    ],
    'Software & Subscriptions': [
        'adobe', 'microsoft office', 'microsoft windows', 'meta quest',
    ],
    'Health & Fitness': [
        'fitness time', 'kunooz pharmacy',
    ],
    'Entertainment & Leisure': [
        'vox cinemas', 'kidzania', 'sparky', 'sala city',
    ],
    'Transportation': [
        'uber driver', 'petromin', 'sasco', 'flyin',
    ],
}

# Default tier configuration: % of OneCard's margin shared with customer
# Higher share = bigger discount for customer
DEFAULT_TIERS = [
    {'name': 'Diamond',  'margin_share_pct': 60, 'color': '#8b5cf6'},
    {'name': 'Gold',     'margin_share_pct': 50, 'color': '#f59e0b'},
    {'name': 'Silver',   'margin_share_pct': 40, 'color': '#9ca3af'},
    {'name': 'Bronze',   'margin_share_pct': 30, 'color': '#b45309'},
    {'name': 'Starter',  'margin_share_pct': 20, 'color': '#3b82f6'},
]


# =============================================================================
# SECTION 2: PRODUCT CLASSIFICATION
# =============================================================================

def classify_country(row):
    """Classify a product to its target country/region."""
    merchant = str(row.get('Merchant Name ', '')).lower().strip()
    product = str(row.get('Product Name', '')).lower().strip()
    currency = str(row.get('Product Currency', '')).strip()

    if 'esim' in merchant or 'e-sim' in merchant:
        esim_country = merchant.replace('- esim', '').replace('-esim', '').strip().title()
        if esim_country:
            return f"eSIM - {esim_country}"

    for country, keywords in COUNTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in merchant or kw in product:
                return country

    if 'gcc' in merchant or 'gcc' in product:
        return 'GCC'
    if 'mena' in merchant or 'mena' in product:
        return 'MENA'

    return CURRENCY_TO_COUNTRY.get(currency, 'Unclassified')


def classify_category(row):
    """Classify a product into a category."""
    merchant = str(row.get('Merchant Name ', '')).lower().strip()
    product = str(row.get('Product Name', '')).lower().strip()
    combined = merchant + ' ' + product

    for category, keywords in CATEGORY_RULES.items():
        for kw in keywords:
            if kw in combined:
                return category
    return 'Other'


def classify_region(country):
    """Group countries into broader regions."""
    gcc = ['Saudi Arabia', 'UAE', 'Kuwait', 'Bahrain', 'Qatar', 'Oman']
    levant = ['Jordan', 'Lebanon', 'Iraq', 'Palestine']
    north_africa = ['Egypt', 'Morocco', 'Tunisia', 'Libya', 'Algeria', 'Sudan']

    if country in gcc or country == 'GCC':
        return 'GCC'
    elif country in levant:
        return 'Levant'
    elif country in north_africa:
        return 'North Africa'
    elif country in ['Europe', 'France', 'UK', 'Germany', 'South Africa']:
        return 'Europe & Africa'
    elif country in ['USA', 'Canada']:
        return 'Americas'
    elif country == 'Global':
        return 'Global'
    elif country == 'MENA':
        return 'MENA'
    elif str(country).startswith('eSIM'):
        return 'eSIM'
    else:
        return 'Other'


def enrich_catalogue(df):
    """Add classifications and compute margin data."""
    print("  Classifying products by country/region...")
    df['Target_Country'] = df.apply(classify_country, axis=1)
    df['Region'] = df['Target_Country'].apply(classify_region)

    print("  Classifying products by category...")
    df['Category'] = df.apply(classify_category, axis=1)

    # Numeric prices
    df['Cost'] = pd.to_numeric(df['Cost Price'], errors='coerce').fillna(0)
    df['Face_Value'] = pd.to_numeric(df['Recommended Retail Price (Resellers currency)'], errors='coerce').fillna(0)
    df['Default_Price'] = pd.to_numeric(df['Default Reseller Price'], errors='coerce').fillna(0)

    # OneCard's margin (what OC earns per unit)
    df['OC_Margin'] = df['Default_Price'] - df['Cost']
    df['OC_Margin_Pct'] = np.where(
        df['Default_Price'] > 0,
        (df['OC_Margin'] / df['Default_Price'] * 100).round(2),
        0
    )

    return df


# =============================================================================
# SECTION 3: TIER PRICING
# =============================================================================

def generate_tier_prices(df, tiers=None):
    """Compute tier prices: customer gets X% of OneCard's margin as discount."""
    if tiers is None:
        tiers = DEFAULT_TIERS

    print(f"\n  Generating prices for {len(tiers)} tiers...")
    for tier in tiers:
        name = tier['name']
        share_pct = tier['margin_share_pct'] / 100.0  # fraction given to customer
        col = f"Price_{name}"

        # Customer discount = OC_Margin * share_pct
        # Customer price = Default_Price - discount = Default_Price - (OC_Margin * share)
        # Which simplifies to: Cost + OC_Margin * (1 - share)
        df[col] = (df['Default_Price'] - df['OC_Margin'] * share_pct).round(2)

        # Floor at cost (never sell below cost)
        df[col] = np.maximum(df[col], df['Cost'])

        # Remaining OC profit at this tier
        profit_col = f"OC_Profit_{name}"
        df[profit_col] = (df[col] - df['Cost']).round(2)

        avg_discount = (df['OC_Margin'] * share_pct).mean()
        print(f"    {name:10s}: {tier['margin_share_pct']}% margin share | Avg discount: {avg_discount:.2f}")

    return df


# =============================================================================
# SECTION 4: MERCHANT AGGREGATION
# =============================================================================

def aggregate_merchants(df):
    """Build per-merchant summary."""
    print("\n  Aggregating merchant data...")
    merchant_col = 'Merchant Name '

    merchants = df.groupby(merchant_col).agg(
        product_count=('Product ID', 'count'),
        categories=('Category', lambda x: list(x.unique())),
        countries=('Target_Country', lambda x: list(x.unique())),
        regions=('Region', lambda x: list(x.unique())),
        avg_cost=('Cost', 'mean'),
        avg_default_price=('Default_Price', 'mean'),
        avg_margin=('OC_Margin', 'mean'),
        avg_margin_pct=('OC_Margin_Pct', 'mean'),
        currency=('Product Currency', 'first'),
        merchant_id=('Merchant id', 'first'),
    ).reset_index()

    merchants = merchants.sort_values('product_count', ascending=False)
    print(f"    {len(merchants)} unique merchants")
    return merchants


# =============================================================================
# SECTION 5: COVERAGE ANALYSIS
# =============================================================================

def compute_coverage(df):
    """Country x Category coverage matrix."""
    print("\n  Computing coverage analysis...")

    coverage = df.groupby(['Region', 'Target_Country', 'Category']).agg(
        product_count=('Product ID', 'count'),
        avg_margin_pct=('OC_Margin_Pct', 'mean'),
    ).reset_index()
    coverage = coverage[~coverage['Target_Country'].str.startswith('eSIM', na=False)]

    country_summary = df.groupby(['Target_Country', 'Region']).agg(
        total_products=('Product ID', 'count'),
        categories=('Category', 'nunique'),
        merchants=('Merchant Name ', 'nunique'),
        avg_margin_pct=('OC_Margin_Pct', 'mean'),
    ).reset_index().sort_values('total_products', ascending=False)

    return coverage, country_summary


# =============================================================================
# SECTION 6: EXCEL OUTPUT
# =============================================================================

def write_excel(df, merchants_df, country_summary, tiers=None):
    """Write the organized Excel workbook."""
    if tiers is None:
        tiers = DEFAULT_TIERS

    output_path = os.path.join(DATA_DIR, 'Organized_Catalogue_Master.xlsx')
    print(f"\n  Writing Excel to {output_path}...")

    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:

        # Overview
        overview = pd.DataFrame({
            'Metric': ['Total Products', 'Total Merchants', 'Countries/Regions',
                       'Product Categories', 'Pricing Tiers'],
            'Value': [len(df), df['Merchant Name '].nunique(),
                      df['Target_Country'].nunique(), df['Category'].nunique(),
                      len(tiers)]
        })
        overview.to_excel(writer, sheet_name='Overview', index=False)

        # By Country
        cols_country = ['Product ID', 'Product Name', 'Merchant Name ', 'Category',
                        'Target_Country', 'Region', 'Product Currency',
                        'Cost', 'Default_Price', 'Face_Value', 'OC_Margin', 'OC_Margin_Pct']
        by_country = df[cols_country].sort_values(['Region', 'Target_Country', 'Category']).copy()
        by_country.columns = ['Product ID', 'Product Name', 'Merchant', 'Category',
                              'Country', 'Region', 'Currency',
                              'Cost', 'Default Price', 'Face Value', 'OC Margin', 'OC Margin %']
        by_country.to_excel(writer, sheet_name='By Country', index=False)

        # By Category
        by_cat = df[cols_country].sort_values(['Category', 'Target_Country']).copy()
        by_cat.columns = by_country.columns
        by_cat.to_excel(writer, sheet_name='By Category', index=False)

        # By Merchant
        merch_out = merchants_df.copy()
        merch_out.columns = ['Merchant', 'Products', 'Categories', 'Countries', 'Regions',
                             'Avg Cost', 'Avg Default Price', 'Avg Margin', 'Avg Margin %',
                             'Currency', 'Merchant ID']
        # Convert lists to strings
        for c in ['Categories', 'Countries', 'Regions']:
            merch_out[c] = merch_out[c].apply(lambda x: ', '.join(str(i) for i in x) if isinstance(x, list) else x)
        merch_out.to_excel(writer, sheet_name='By Merchant', index=False)

        # Tier price sheets
        for tier in tiers:
            name = tier['name']
            price_col = f'Price_{name}'
            profit_col = f'OC_Profit_{name}'
            tier_df = df[['Product ID', 'Product Name', 'Merchant Name ', 'Category',
                          'Target_Country', 'Product Currency', 'Cost', 'Default_Price',
                          'Face_Value', 'OC_Margin', price_col, profit_col]].copy()
            tier_df.columns = ['Product ID', 'Product Name', 'Merchant', 'Category',
                               'Country', 'Currency', 'Cost', 'Default Price',
                               'Face Value', 'OC Margin',
                               f'{name} Price', f'OC Profit ({name})']
            tier_df[f'Margin Share'] = f"{tier['margin_share_pct']}%"
            tier_df = tier_df.sort_values(['Country', 'Category'])
            tier_df.to_excel(writer, sheet_name=f'Tier - {name}', index=False)

        # Coverage
        country_summary.to_excel(writer, sheet_name='Coverage Analysis', index=False)

        # Raw
        df.to_excel(writer, sheet_name='Raw Enriched Data', index=False)

    print(f"    [OK] Excel saved: {output_path}")
    return output_path


# =============================================================================
# SECTION 7: JSON OUTPUT
# =============================================================================

def write_json(df, merchants_df, coverage_df, country_summary, tiers=None):
    """Write JSON for the dashboard."""
    if tiers is None:
        tiers = DEFAULT_TIERS

    output_path = os.path.join(DATA_DIR, 'catalogue_data.json')
    print(f"\n  Writing JSON to {output_path}...")

    # Products
    base_cols = ['Product ID', 'Product Name', 'Merchant Name ', 'Merchant id', 'Category',
                 'Target_Country', 'Region', 'Product Currency',
                 'Cost', 'Default_Price', 'Face_Value', 'OC_Margin', 'OC_Margin_Pct']
    tier_price_cols = [f'Price_{t["name"]}' for t in tiers]
    tier_profit_cols = [f'OC_Profit_{t["name"]}' for t in tiers]

    products = df[base_cols + tier_price_cols + tier_profit_cols].copy()
    products.columns = (
        ['product_id', 'product_name', 'merchant', 'merchant_id', 'category',
         'country', 'region', 'currency',
         'cost', 'default_price', 'face_value', 'oc_margin', 'oc_margin_pct']
        + [f'price_{t["name"].lower()}' for t in tiers]
        + [f'profit_{t["name"].lower()}' for t in tiers]
    )
    products = products.where(pd.notnull(products), None)

    # Merchants
    merch_list = []
    for _, m in merchants_df.iterrows():
        merch_list.append({
            'name': m['Merchant Name '],
            'merchant_id': int(m['merchant_id']) if pd.notna(m['merchant_id']) else None,
            'product_count': int(m['product_count']),
            'categories': m['categories'] if isinstance(m['categories'], list) else [],
            'countries': m['countries'] if isinstance(m['countries'], list) else [],
            'regions': m['regions'] if isinstance(m['regions'], list) else [],
            'avg_margin_pct': round(float(m['avg_margin_pct']), 2) if pd.notna(m['avg_margin_pct']) else 0,
            'currency': m['currency'],
        })

    # Country stats
    country_stats = {}
    for _, row in country_summary.iterrows():
        c = row['Target_Country']
        if str(c).startswith('eSIM'):
            continue
        country_stats[c] = {
            'total_products': int(row['total_products']),
            'categories': int(row['categories']),
            'merchants': int(row['merchants']),
            'region': row['Region'],
            'avg_margin_pct': round(float(row['avg_margin_pct']), 2),
        }

    # Category stats
    cat_stats = df.groupby('Category').agg(
        count=('Product ID', 'count'),
        merchants=('Merchant Name ', 'nunique'),
        countries=('Target_Country', 'nunique'),
        avg_margin=('OC_Margin_Pct', 'mean'),
    ).reset_index()

    data = {
        'products': products.to_dict(orient='records'),
        'merchants': merch_list,
        'country_stats': country_stats,
        'category_stats': cat_stats.to_dict(orient='records'),
        'default_tiers': tiers,
        'coverage': coverage_df.to_dict(orient='records'),
        'all_countries': sorted(df['Target_Country'].dropna().unique().tolist()),
        'all_categories': sorted(df['Category'].dropna().unique().tolist()),
        'all_regions': sorted(df['Region'].dropna().unique().tolist()),
        'all_merchants': sorted(df['Merchant Name '].dropna().unique().tolist()),
        'all_currencies': sorted(df['Product Currency'].dropna().unique().tolist()),
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, default=str)

    print(f"    [OK] JSON saved: {output_path}")
    print(f"    Products: {len(data['products'])}")
    print(f"    Merchants: {len(data['merchants'])}")
    print(f"    Countries: {len(data['all_countries'])}")
    return output_path


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("  CATALOGUE MANAGEMENT SYSTEM v2 - ENGINE")
    print("=" * 70)

    # 1. Load
    print("\n[1/5] Loading Full Catalogue...")
    catalogue = pd.read_excel(
        os.path.join(DATA_DIR, 'Full Catalogue.xls'),
        sheet_name='Reseller_prices_Report'
    )
    print(f"    Loaded {len(catalogue)} products")

    # 2. Classify
    print("\n[2/5] Classifying products...")
    catalogue = enrich_catalogue(catalogue)

    print("\n  --- Country Distribution (top 15) ---")
    for c, n in catalogue['Target_Country'].value_counts().head(15).items():
        print(f"    {c:30s}: {n:5d}")

    print("\n  --- Category Distribution ---")
    for c, n in catalogue['Category'].value_counts().items():
        print(f"    {c:30s}: {n:5d}")

    print("\n  --- Region Distribution ---")
    for r, n in catalogue['Region'].value_counts().items():
        print(f"    {r:30s}: {n:5d}")

    # 3. Tier pricing
    print("\n[3/5] Generating tier prices...")
    catalogue = generate_tier_prices(catalogue)

    # 4. Merchant aggregation
    print("\n[4/5] Aggregating merchants...")
    merchants = aggregate_merchants(catalogue)

    # 5. Coverage
    coverage, country_summary = compute_coverage(catalogue)

    # 6. Output
    print("\n[5/5] Writing output files...")
    write_excel(catalogue, merchants, country_summary)
    write_json(catalogue, merchants, coverage, country_summary)

    print("\n" + "=" * 70)
    print("  COMPLETE!")
    print("=" * 70)


if __name__ == '__main__':
    main()
