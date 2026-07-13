"""
Catalogue Management System — Engine
=====================================
Reads the Full Catalogue, classifies products by country/region and category,
loads historical data for customer tiering, generates tiered pricing,
coverage analysis, and outputs:
  1. Organized_Catalogue_Master.xlsx (structured Excel workbook)
  2. catalogue_data.json (JSON data file for the web dashboard)
"""

import pandas as pd
import numpy as np
import json
import os
import re
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

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

# Keywords in merchant/product names → country mapping
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

# Product category classification rules
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
        'l\'occitane', 'rituals', 'magrabi', 'golden scent', 'haddad',
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

# Customer tier thresholds (annual revenue in SAR)
TIER_CONFIG = {
    'Platinum': {'min_revenue': 10_000_000, 'discount_pct': 4.0, 'color': '#8B5CF6'},
    'Gold':     {'min_revenue':  3_000_000, 'discount_pct': 2.5, 'color': '#F59E0B'},
    'Silver':   {'min_revenue':    500_000, 'discount_pct': 1.5, 'color': '#6B7280'},
    'Bronze':   {'min_revenue':    100_000, 'discount_pct': 0.75, 'color': '#B45309'},
    'Standard': {'min_revenue':          0, 'discount_pct': 0.0, 'color': '#3B82F6'},
}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: PRODUCT CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

def classify_country(row):
    """Classify a product to its target country/region using multi-signal approach."""
    merchant = str(row.get('Merchant Name ', '')).lower().strip()
    product = str(row.get('Product Name', '')).lower().strip()
    currency = str(row.get('Product Currency', '')).strip()

    # Signal 1: Check for eSIM products (merchant name ends with "- eSim" pattern)
    if 'esim' in merchant or 'e-sim' in merchant:
        # Extract country from merchant name
        esim_country = merchant.replace('- esim', '').replace('-esim', '').strip().title()
        if esim_country:
            return f"eSIM - {esim_country}"

    # Signal 2: Check merchant name for country keywords (most specific)
    for country, keywords in COUNTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in merchant or kw in product:
                return country

    # Signal 3: GCC-specific patterns
    if 'gcc' in merchant or 'gcc' in product:
        return 'GCC'
    if 'mena' in merchant or 'mena' in product:
        return 'MENA'

    # Signal 4: Fall back to currency
    country = CURRENCY_TO_COUNTRY.get(currency, 'Unclassified')

    return country


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


def classify_sub_region(country):
    """Group countries into broader regions."""
    gcc = ['Saudi Arabia', 'UAE', 'Kuwait', 'Bahrain', 'Qatar', 'Oman']
    levant = ['Jordan', 'Lebanon', 'Iraq', 'Palestine']
    north_africa = ['Egypt', 'Morocco', 'Tunisia', 'Libya', 'Algeria', 'Sudan']
    europe = ['Europe', 'France', 'UK', 'Germany', 'South Africa']
    americas = ['USA', 'Canada']

    if country in gcc:
        return 'GCC'
    elif country in levant:
        return 'Levant'
    elif country in north_africa:
        return 'North Africa'
    elif country in europe:
        return 'Europe & Africa'
    elif country in americas:
        return 'Americas'
    elif country == 'Global':
        return 'Global'
    elif country == 'GCC':
        return 'GCC'
    elif country == 'MENA':
        return 'MENA'
    elif str(country).startswith('eSIM'):
        return 'eSIM'
    else:
        return 'Other'


def enrich_catalogue(df):
    """Add country, region, and category classifications to the catalogue."""
    print("  Classifying products by country/region...")
    df['Target_Country'] = df.apply(classify_country, axis=1)
    df['Region'] = df['Target_Country'].apply(classify_sub_region)

    print("  Classifying products by category...")
    df['Category'] = df.apply(classify_category, axis=1)

    # Calculate margins
    df['Cost_Price_Num'] = pd.to_numeric(df['Cost Price'], errors='coerce').fillna(0)
    default_price_col = 'Default Reseller Price'
    df['Default_Price_Num'] = pd.to_numeric(df[default_price_col], errors='coerce').fillna(0)
    df['Margin_Pct'] = np.where(
        df['Default_Price_Num'] > 0,
        ((df['Default_Price_Num'] - df['Cost_Price_Num']) / df['Default_Price_Num'] * 100).round(2),
        0
    )

    return df


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: CUSTOMER TIERING
# ═══════════════════════════════════════════════════════════════════════════════

def load_historical_data():
    """Load 2024 + 2025 historical data and aggregate per customer."""
    print("\n  Loading 2024 data...")
    df24 = pd.read_csv(
        os.path.join(DATA_DIR, '2024 Data.csv'),
        usecols=['Country', 'Customer_ID_DR', 'Customer', 'Merchant',
                 ' Total Sales ', ' Total Cost ', ' Rep ']
    )
    for col in [' Total Sales ', ' Total Cost ']:
        df24[col] = pd.to_numeric(
            df24[col].astype(str).str.replace(',', '').str.strip(),
            errors='coerce'
        ).fillna(0)
    df24 = df24.rename(columns={
        ' Total Sales ': 'total_sales',
        ' Total Cost ': 'total_cost',
        ' Rep ': 'rep'
    })
    df24['year'] = 2024

    print("  Loading 2025 data...")
    df25 = pd.read_excel(
        os.path.join(DATA_DIR, '2025 Report (1).xlsx'),
        sheet_name='DATA'
    )
    for col in ['Total Sales', 'Total Cost']:
        df25[col] = pd.to_numeric(df25[col], errors='coerce').fillna(0)
    df25 = df25.rename(columns={
        'Customer Name': 'Customer',
        'Total Sales': 'total_sales',
        'Total Cost': 'total_cost',
        'Sales Man': 'rep',
        'Item_Name': 'Item_Name',
    })
    df25['year'] = 2025

    # Combine
    cols = ['Country', 'Customer_ID_DR', 'Customer', 'total_sales', 'total_cost', 'rep', 'year']
    combined = pd.concat([
        df24[['Country', 'Customer_ID_DR', 'Customer', 'total_sales', 'total_cost', 'rep', 'year']],
        df25[['Country', 'Customer_ID_DR', 'Customer', 'total_sales', 'total_cost', 'rep', 'year']],
    ], ignore_index=True)

    return combined


def compute_customer_tiers(historical_df):
    """Compute customer tiers based on average annual revenue."""
    print("\n  Computing customer tiers...")

    # Aggregate per customer per year
    yearly = historical_df.groupby(['Customer_ID_DR', 'Customer', 'Country', 'year']).agg(
        annual_sales=('total_sales', 'sum'),
        annual_cost=('total_cost', 'sum'),
    ).reset_index()

    # Get the latest rep assignment
    latest_rep = historical_df.sort_values('year').drop_duplicates(
        subset=['Customer_ID_DR'], keep='last'
    )[['Customer_ID_DR', 'rep']]

    # Average across years
    customer_agg = yearly.groupby(['Customer_ID_DR', 'Customer', 'Country']).agg(
        avg_annual_sales=('annual_sales', 'mean'),
        total_sales_all=('annual_sales', 'sum'),
        avg_annual_cost=('annual_cost', 'mean'),
        years_active=('year', 'nunique'),
        latest_year=('year', 'max'),
    ).reset_index()

    customer_agg = customer_agg.merge(latest_rep, on='Customer_ID_DR', how='left')
    customer_agg['avg_annual_gp'] = customer_agg['avg_annual_sales'] - customer_agg['avg_annual_cost']
    customer_agg['avg_margin_pct'] = np.where(
        customer_agg['avg_annual_sales'] > 0,
        (customer_agg['avg_annual_gp'] / customer_agg['avg_annual_sales'] * 100).round(2),
        0
    )

    # Assign tiers
    def assign_tier(revenue):
        for tier_name, config in TIER_CONFIG.items():
            if revenue >= config['min_revenue']:
                return tier_name
        return 'Standard'

    customer_agg['Tier'] = customer_agg['avg_annual_sales'].apply(assign_tier)
    customer_agg['Discount_Pct'] = customer_agg['Tier'].map(
        {t: c['discount_pct'] for t, c in TIER_CONFIG.items()}
    )

    # Sort by sales descending
    customer_agg = customer_agg.sort_values('avg_annual_sales', ascending=False)

    print(f"    Total customers: {len(customer_agg)}")
    for tier_name in TIER_CONFIG:
        count = (customer_agg['Tier'] == tier_name).sum()
        total_rev = customer_agg.loc[customer_agg['Tier'] == tier_name, 'avg_annual_sales'].sum()
        print(f"    {tier_name:12s}: {count:5d} customers | Avg Annual Rev: SAR {total_rev:>15,.0f}")

    return customer_agg


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: TIERED PRICING
# ═══════════════════════════════════════════════════════════════════════════════

def generate_tiered_prices(catalogue_df):
    """Generate tier-specific prices for each product."""
    print("\n  Generating tiered price lists...")
    base_price_col = 'Default_Price_Num'

    for tier_name, config in TIER_CONFIG.items():
        discount = config['discount_pct'] / 100.0
        col_name = f"Price_{tier_name}"
        catalogue_df[col_name] = (catalogue_df[base_price_col] * (1 - discount)).round(2)
        # Ensure tier price doesn't go below cost
        catalogue_df[col_name] = np.maximum(catalogue_df[col_name], catalogue_df['Cost_Price_Num'])

    return catalogue_df


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: COVERAGE ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_coverage_analysis(catalogue_df, historical_df):
    """Analyze coverage gaps: countries × categories."""
    print("\n  Computing coverage analysis...")

    # Products available by country × category
    product_coverage = catalogue_df.groupby(['Target_Country', 'Category']).agg(
        product_count=('Product ID', 'count'),
        avg_margin=('Margin_Pct', 'mean'),
    ).reset_index()

    # Historical sales by country
    sales_by_country = historical_df.groupby('Country').agg(
        total_sales=('total_sales', 'sum'),
        unique_customers=('Customer_ID_DR', 'nunique'),
    ).reset_index()

    # Country summary
    country_summary = catalogue_df.groupby(['Target_Country', 'Region']).agg(
        total_products=('Product ID', 'count'),
        categories=('Category', 'nunique'),
        merchants=('Merchant Name ', 'nunique'),
        avg_cost=('Cost_Price_Num', 'mean'),
    ).reset_index().sort_values('total_products', ascending=False)

    return product_coverage, sales_by_country, country_summary


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: EXCEL OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════

def write_excel_output(catalogue_df, customer_tiers, product_coverage, sales_by_country, country_summary):
    """Write the organized Excel workbook."""
    output_path = os.path.join(DATA_DIR, 'Organized_Catalogue_Master.xlsx')
    print(f"\n  Writing Excel workbook to {output_path}...")

    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:

        # --- Sheet 1: Overview ---
        overview_data = {
            'Metric': [
                'Total Products', 'Total Merchants', 'Total Countries/Regions',
                'Total Categories', 'Total Customers (Historical)',
                'Platinum Customers', 'Gold Customers', 'Silver Customers',
                'Bronze Customers', 'Standard Customers',
            ],
            'Value': [
                len(catalogue_df),
                catalogue_df['Merchant Name '].nunique(),
                catalogue_df['Target_Country'].nunique(),
                catalogue_df['Category'].nunique(),
                len(customer_tiers),
                (customer_tiers['Tier'] == 'Platinum').sum(),
                (customer_tiers['Tier'] == 'Gold').sum(),
                (customer_tiers['Tier'] == 'Silver').sum(),
                (customer_tiers['Tier'] == 'Bronze').sum(),
                (customer_tiers['Tier'] == 'Standard').sum(),
            ]
        }
        pd.DataFrame(overview_data).to_excel(writer, sheet_name='Overview', index=False)

        # --- Sheet 2: By Country ---
        country_cols = ['Product ID', 'Product Name', 'Merchant Name ', 'Category',
                        'Target_Country', 'Region', 'Product Currency',
                        'Cost_Price_Num', 'Default_Price_Num', 'Margin_Pct']
        by_country = catalogue_df[country_cols].sort_values(['Region', 'Target_Country', 'Category'])
        by_country.columns = ['Product ID', 'Product Name', 'Merchant', 'Category',
                              'Country', 'Region', 'Currency', 'Cost Price', 'Default Price', 'Margin %']
        by_country.to_excel(writer, sheet_name='By Country', index=False)

        # --- Sheet 3: By Category ---
        by_category = catalogue_df[country_cols].sort_values(['Category', 'Target_Country'])
        by_category.columns = ['Product ID', 'Product Name', 'Merchant', 'Category',
                                'Country', 'Region', 'Currency', 'Cost Price', 'Default Price', 'Margin %']
        by_category.to_excel(writer, sheet_name='By Category', index=False)

        # --- Sheets 4-8: Tier Price Lists ---
        tier_cols_base = ['Product ID', 'Product Name', 'Merchant Name ', 'Category',
                          'Target_Country', 'Product Currency', 'Cost_Price_Num']
        for tier_name in TIER_CONFIG:
            price_col = f"Price_{tier_name}"
            discount = TIER_CONFIG[tier_name]['discount_pct']
            tier_df = catalogue_df[tier_cols_base + [price_col, 'Default_Price_Num']].copy()
            tier_df['Discount %'] = discount
            tier_df['Margin_vs_Cost'] = np.where(
                tier_df[price_col] > 0,
                ((tier_df[price_col] - tier_df['Cost_Price_Num']) / tier_df[price_col] * 100).round(2),
                0
            )
            tier_df.columns = ['Product ID', 'Product Name', 'Merchant', 'Category',
                               'Country', 'Currency', 'Cost Price',
                               f'{tier_name} Price', 'Default Price', 'Discount %', 'Margin vs Cost %']
            tier_df = tier_df.sort_values(['Country', 'Category'])
            sheet_name = f'Tier - {tier_name}'
            tier_df.to_excel(writer, sheet_name=sheet_name, index=False)

        # --- Sheet 9: Customer Tiers ---
        cust_out = customer_tiers[[
            'Customer_ID_DR', 'Customer', 'Country', 'Tier', 'Discount_Pct',
            'avg_annual_sales', 'avg_annual_gp', 'avg_margin_pct',
            'years_active', 'rep'
        ]].copy()
        cust_out.columns = ['Customer ID', 'Customer Name', 'Country', 'Tier', 'Discount %',
                            'Avg Annual Sales', 'Avg Annual GP', 'Avg Margin %',
                            'Years Active', 'Sales Rep']
        cust_out.to_excel(writer, sheet_name='Customer Tiers', index=False)

        # --- Sheet 10: Coverage Analysis ---
        country_summary.to_excel(writer, sheet_name='Coverage Analysis', index=False)

        # --- Sheet 11: Raw Enriched Data ---
        catalogue_df.to_excel(writer, sheet_name='Raw Enriched Data', index=False)

    print(f"    [OK] Excel workbook saved: {output_path}")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: JSON OUTPUT FOR DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

def write_json_output(catalogue_df, customer_tiers, country_summary):
    """Write JSON data for the web dashboard."""
    output_path = os.path.join(DATA_DIR, 'catalogue_data.json')
    print(f"\n  Writing JSON data for dashboard to {output_path}...")

    # Products
    product_cols = ['Product ID', 'Product Name', 'Merchant Name ', 'Category',
                    'Target_Country', 'Region', 'Product Currency',
                    'Cost_Price_Num', 'Default_Price_Num', 'Margin_Pct']
    tier_price_cols = [f'Price_{t}' for t in TIER_CONFIG]
    products = catalogue_df[product_cols + tier_price_cols].copy()
    products.columns = ['product_id', 'product_name', 'merchant', 'category',
                        'country', 'region', 'currency',
                        'cost_price', 'default_price', 'margin_pct'] + \
                       [f'price_{t.lower()}' for t in TIER_CONFIG]

    # Replace NaN with None for JSON
    products = products.where(pd.notnull(products), None)

    # Country stats
    country_stats = {}
    for _, row in country_summary.iterrows():
        country = row['Target_Country']
        if str(country).startswith('eSIM'):
            continue  # Skip individual eSIM countries
        country_stats[country] = {
            'total_products': int(row['total_products']),
            'categories': int(row['categories']),
            'merchants': int(row['merchants']),
            'region': row['Region'],
        }

    # Category stats
    cat_stats = catalogue_df.groupby('Category').agg(
        count=('Product ID', 'count'),
        merchants=('Merchant Name ', 'nunique'),
        countries=('Target_Country', 'nunique'),
    ).reset_index()

    # Customer tiers
    tier_summary = {}
    for tier_name, config in TIER_CONFIG.items():
        tier_customers = customer_tiers[customer_tiers['Tier'] == tier_name]
        tier_summary[tier_name] = {
            'count': int(len(tier_customers)),
            'total_revenue': float(tier_customers['avg_annual_sales'].sum()),
            'avg_revenue': float(tier_customers['avg_annual_sales'].mean()) if len(tier_customers) > 0 else 0,
            'discount_pct': config['discount_pct'],
            'color': config['color'],
            'min_revenue': config['min_revenue'],
        }

    # Customer list (top 200 for dashboard)
    top_customers = customer_tiers.head(200)[[
        'Customer_ID_DR', 'Customer', 'Country', 'Tier', 'Discount_Pct',
        'avg_annual_sales', 'avg_annual_gp', 'avg_margin_pct', 'years_active', 'rep'
    ]].copy()
    top_customers.columns = ['id', 'name', 'country', 'tier', 'discount_pct',
                             'avg_sales', 'avg_gp', 'avg_margin', 'years_active', 'rep']
    top_customers = top_customers.where(pd.notnull(top_customers), None)

    # Coverage matrix
    coverage = catalogue_df.groupby(['Region', 'Target_Country', 'Category']).size().reset_index(name='count')
    coverage = coverage[~coverage['Target_Country'].str.startswith('eSIM', na=False)]

    # Assemble JSON
    data = {
        'products': products.to_dict(orient='records'),
        'country_stats': country_stats,
        'category_stats': cat_stats.to_dict(orient='records'),
        'tier_summary': tier_summary,
        'tier_config': {k: {'discount_pct': v['discount_pct'], 'min_revenue': v['min_revenue'], 'color': v['color']}
                        for k, v in TIER_CONFIG.items()},
        'customers': top_customers.to_dict(orient='records'),
        'coverage': coverage.to_dict(orient='records'),
        'all_countries': sorted(catalogue_df['Target_Country'].dropna().unique().tolist()),
        'all_categories': sorted(catalogue_df['Category'].dropna().unique().tolist()),
        'all_regions': sorted(catalogue_df['Region'].dropna().unique().tolist()),
        'all_merchants': sorted(catalogue_df['Merchant Name '].dropna().unique().tolist()),
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, default=str)

    print(f"    [OK] JSON data saved: {output_path}")
    print(f"    Products: {len(data['products'])}")
    print(f"    Countries: {len(data['all_countries'])}")
    print(f"    Categories: {len(data['all_categories'])}")
    print(f"    Customers: {len(data['customers'])}")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  CATALOGUE MANAGEMENT SYSTEM - ENGINE")
    print("=" * 70)

    # Step 1: Load and enrich catalogue
    print("\n[1/6] Loading Full Catalogue...")
    catalogue = pd.read_excel(
        os.path.join(DATA_DIR, 'Full Catalogue.xls'),
        sheet_name='Reseller_prices_Report'
    )
    print(f"    Loaded {len(catalogue)} products")

    # Step 2: Classify
    print("\n[2/6] Classifying products...")
    catalogue = enrich_catalogue(catalogue)

    # Print classification summary
    print("\n  --- Country Distribution ---")
    for country, count in catalogue['Target_Country'].value_counts().head(20).items():
        print(f"    {country:30s}: {count:5d} products")

    print("\n  --- Category Distribution ---")
    for cat, count in catalogue['Category'].value_counts().items():
        print(f"    {cat:30s}: {count:5d} products")

    print("\n  --- Region Distribution ---")
    for reg, count in catalogue['Region'].value_counts().items():
        print(f"    {reg:30s}: {count:5d} products")

    # Step 3: Load historical data & compute tiers
    print("\n[3/6] Loading historical data for customer tiering...")
    historical = load_historical_data()
    print(f"    Loaded {len(historical)} rows across {historical['year'].nunique()} years")

    print("\n[4/6] Computing customer tiers...")
    customer_tiers = compute_customer_tiers(historical)

    # Step 4: Generate tiered prices
    print("\n[5/6] Generating tiered pricing...")
    catalogue = generate_tiered_prices(catalogue)

    # Step 5: Coverage analysis
    product_coverage, sales_by_country, country_summary = compute_coverage_analysis(catalogue, historical)

    # Step 6: Write outputs
    print("\n[6/6] Writing output files...")
    excel_path = write_excel_output(catalogue, customer_tiers, product_coverage, sales_by_country, country_summary)
    json_path = write_json_output(catalogue, customer_tiers, country_summary)

    print("\n" + "=" * 70)
    print("  COMPLETE!")
    print(f"  Excel: {excel_path}")
    print(f"  JSON:  {json_path}")
    print("=" * 70)


if __name__ == '__main__':
    main()
