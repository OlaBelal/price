import requests
import os
import time
from dotenv import load_dotenv
import json  # We need this for GraphQL

# Load environment variables from .env file
load_dotenv()

# ==== SETTINGS ====
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_TOKEN")
LOCATION_ID = os.getenv("LOCATION_ID")
POS_BASE_URL = os.getenv("POS_BASE_URL")
POS_PASSWORD = os.getenv("POS_PASSWORD")


# ==== HELPER FUNCTIONS ====
def sanitize_sku(sku):
    """Removes non-printable characters from a SKU string."""
    if not isinstance(sku, str):
        return ""
    return "".join(char for char in sku if char.isprintable())


def round_to_5_or_10(price):
    """
    Round price UP to the nearest 5 or 10 Egyptian Pounds.
    Returns an integer price in pounds.
    """
    # Convert to Egyptian pounds and round up to nearest integer
    price_pounds = int(price)  # Assuming price is already in pounds
    
    # Check if price already ends with 0 or 5
    last_digit = price_pounds % 10
    if last_digit == 0 or last_digit == 5:
        return price_pounds
    
    # Round up to nearest 5 or 10
    remainder = price_pounds % 10
    
    if remainder < 5:
        price_pounds = price_pounds - remainder + 5
    else:
        price_pounds = price_pounds - remainder + 10
    
    return price_pounds


# ==== API FUNCTIONS ====
def get_shopify_skus():
    """Get all products with SKUs, prices, and compare_at_prices from Shopify using cursor-based pagination."""
    skus = []
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-07/products.json?limit=250"
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}

    while url:
        try:
            resp = requests.get(url, headers=headers)
            resp.raise_for_status()
            products = resp.json().get("products", [])
            for product in products:
                for variant in product["variants"]:
                    if variant.get("sku") and variant.get("inventory_item_id"):
                        skus.append({
                            "sku": variant["sku"],
                            "inventory_item_id": variant["inventory_item_id"],
                            "variant_id": variant["id"],
                            "current_price": variant.get("price", "0.0"),
                            "compare_at_price": variant.get("compare_at_price")
                        })

            link_header = resp.headers.get("Link")
            if link_header and 'rel="next"' in link_header:
                parts = link_header.split(",")
                next_link_info = [p for p in parts if 'rel="next"' in p]
                if next_link_info:
                    url = next_link_info[0].split(';')[0].strip('<> ')
                else:
                    url = None
            else:
                url = None
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching products from Shopify: {e}")
            return None
    print(f"✅ Retrieved {len(skus)} SKUs from Shopify")
    return skus


def get_all_pos_inventory():
    """
    Fetches all items from the POS and returns them as a dictionary,
    safely skipping any malformed items.
    """
    print("⏳ Fetching all inventory from POS... (this may take a moment)")
    url = f"{POS_BASE_URL}?ps={POS_PASSWORD}&get=all&output=json&sep=;"
    pos_inventory_map = {}

    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        pos_data = resp.json()

        for item in pos_data:
            # --- THIS IS THE CRITICAL SAFETY CHECK ---
            # Only process items that have both an ID and a Quantity
            if item and "ID" in item and "Qua" in item and "Price" in item:
                sku = str(item["ID"])
                quantity = int(float(item["Qua"]))
                price = float(item.get("Price", 0))
                pos_inventory_map[sku] = {
                    "quantity": quantity,
                    "price": price
                }
            else:
                # Optional: Log the bad data if you want to see it
                # print(f"Skipping malformed item from POS: {item}")
                pass

        print(f"✅ Loaded {len(pos_inventory_map)} items from POS.")
        return pos_inventory_map

    except requests.exceptions.RequestException as e:
        print(f"❌ Critical error fetching inventory from POS: {e}")
        return None
    except (ValueError, TypeError):
        print("❌ Critical error: Could not parse JSON data from POS.")
        return None


def update_shopify_stock(inventory_item_id, sku, quantity):
    """
    Update Shopify stock using the modern GraphQL API.
    This is more reliable than the old REST endpoint.
    """
    # The URL for all GraphQL requests is the same
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-07/graphql.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }

    # This is the GraphQL "mutation" (a query that changes data)
    query = """
    mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
      inventorySetOnHandQuantities(input: $input) {
        userErrors {
          field
          message
        }
        inventoryAdjustmentGroup {
          createdAt
        }
      }
    }
    """

    # These are the variables that get passed into the query
    variables = {
        "input": {
            "reason": "correction",
            "setQuantities": [
                {
                    "inventoryItemId": f"gid://shopify/InventoryItem/{inventory_item_id}",
                    "locationId": f"gid://shopify/Location/{LOCATION_ID}",
                    "quantity": quantity
                }
            ]
        }
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps({"query": query, "variables": variables}), timeout=15)
        resp.raise_for_status()

        response_data = resp.json()
        user_errors = response_data.get("data", {}).get("inventorySetOnHandQuantities", {}).get("userErrors", [])

        if user_errors:
            # If there are "userErrors", it means a logical rejection (like the old 422 error)
            error_message = user_errors[0]['message']
            print(f"⚠️  Shopify rejected stock update for SKU {sku}. Reason: {error_message}")
            return False

        return True  # Success!

    except requests.exceptions.RequestException as e:
        print(f"❌ Error updating Shopify stock for SKU {sku}: {e}")
        return False


def update_shopify_price(variant_id, sku, pos_price, current_shopify_price, compare_at_price):
    """
    Update Shopify price using REST API for the variant.
    Price = POS price + 15%
    Only updates if:
    1. Product has no discount (current_price >= compare_at_price OR compare_at_price is null/empty)
    2. The price needs to be updated (not already increased by 15% from POS price)
    """
    # Check if product has a discount
    # A product has a discount if compare_at_price exists and is greater than current_price
    has_discount = False
    
    if compare_at_price and compare_at_price.strip():
        try:
            compare_price_float = float(compare_at_price)
            current_price_float = float(current_shopify_price)
            
            # If compare price is higher than current price, there's a discount
            if compare_price_float > current_price_float:
                has_discount = True
                print(f"💰 SKU {sku} has a discount (Compare: {compare_price_float}, Current: {current_price_float}). Skipping price update.")
                return False
        except (ValueError, TypeError):
            # If we can't parse the prices, assume no discount
            pass
    
    # Calculate the target price (POS price + 15%)
    target_price = round(pos_price * 1.15, 2)
    
    # Apply rounding to nearest 5 or 10 Egyptian Pounds
    target_price = round_to_5_or_10(target_price)
    
    # Parse current Shopify price to float
    try:
        current_price_float = float(current_shopify_price)
    except (ValueError, TypeError):
        print(f"⚠️  Cannot parse current price for SKU {sku}: {current_shopify_price}")
        return False
    
    # Check if current price is already at or above target price (with small tolerance)
    tolerance = 0.01  # 1 cent tolerance for floating point comparison
    if abs(current_price_float - target_price) <= tolerance or current_price_float > target_price:
        print(f"ℹ️  Price for SKU {sku} already at target ({current_price_float}) or higher, skipping price update")
        return False
    
    # Prepare the request
    url = f"https://{SHOPIFY_STORE}/admin/api/2024-07/variants/{variant_id}.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_TOKEN,
        "Content-Type": "application/json"
    }
    
    data = {
        "variant": {
            "id": variant_id,
            "price": str(target_price)
        }
    }
    
    try:
        resp = requests.put(url, headers=headers, data=json.dumps(data), timeout=15)
        resp.raise_for_status()
        
        print(f"💰 Updated price for SKU {sku}: {current_price_float} → {target_price} (+15% from POS price {pos_price})")
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Error updating price for SKU {sku}: {e}")
        return False


# ==== MAIN ====
def main():
    required_vars = ["SHOPIFY_STORE", "SHOPIFY_TOKEN", "LOCATION_ID", "POS_BASE_URL", "POS_PASSWORD"]
    for var in required_vars:
        if not os.getenv(var):
            print(f"❌ Critical Error: Environment variable '{var}' not found. Please check your .env file.")
            return

    shopify_skus = get_shopify_skus()
    if not shopify_skus:
        print("Could not retrieve SKUs from Shopify. Exiting.")
        return

    pos_inventory = get_all_pos_inventory()
    if not pos_inventory:
        print("Could not retrieve inventory from POS. Exiting.")
        return

    print(f"\nComparing {len(shopify_skus)} Shopify SKUs against {len(pos_inventory)} POS items...")

    for item in shopify_skus:
        original_sku = item["sku"]
        inventory_item_id = item["inventory_item_id"]
        variant_id = item["variant_id"]
        current_shopify_price = item["current_price"]
        compare_at_price = item["compare_at_price"]

        clean_sku = sanitize_sku(original_sku)
        pos_data = pos_inventory.get(clean_sku)

        if pos_data is not None:
            # Update stock
            if update_shopify_stock(inventory_item_id, original_sku, pos_data["quantity"]):
                print(f"✅ Synced stock for SKU {original_sku} → Set Qty to {pos_data['quantity']}")
            
            # Update price (with discount check)
            time.sleep(0.2)  # Sleep between requests
            update_shopify_price(variant_id, original_sku, pos_data["price"], current_shopify_price, compare_at_price)
        else:
            print(f"❌ No match in POS for SKU: {original_sku}")

        time.sleep(0.2)  # Sleep between products to avoid rate limiting


if __name__ == "__main__":
    main()
