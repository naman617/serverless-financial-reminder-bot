import json
import os
import boto3
import requests
import gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials


# --- AWS Clients and Constants ---
# Initialize AWS clients once outside the handler for performance
ssm_client = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')
ses_client = boto3.client('ses')

# Fetch constants from environment variables
# These are set in the Lambda configuration
PARAM_NAME = os.environ.get('GOOGLE_API_PARAM_NAME', 'google-sheets-api-key')
DYNAMODB_TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'ReminderStatus')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
FROM_EMAIL = os.environ.get('FROM_EMAIL')
TO_EMAIL = os.environ.get('TO_EMAIL')

# Get a reference to our DynamoDB table
table = dynamodb.Table(DYNAMODB_TABLE_NAME)

# --- Google Sheets Authentication Helper ---
def get_gspread_client():
    """
    Fetches credentials from AWS Parameter Store and authorizes with Google Sheets.
    """
    print("Fetching Google credentials from Parameter Store...")
    param = ssm_client.get_parameter(Name=PARAM_NAME, WithDecryption=True)
    google_creds_json = json.loads(param['Parameter']['Value'])

    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    credentials = Credentials.from_service_account_info(google_creds_json, scopes=scopes)
    
    print("Authorizing with gspread...")
    return gspread.authorize(credentials)

# --- Replace the old send_telegram_alert function with this one ---
def send_telegram_alert(message):
    """
    Sends a formatted message to a Telegram chat using the requests library.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram environment variables not set. Skipping alert.")
        return

    print(f"Sending Telegram alert...")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown" # Allows for bold, italics, etc. if you want later
    }
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()  # This will raise an error for bad responses (4xx or 5xx)
        print("Telegram alert sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"Error sending Telegram alert: {e}")

# --- Replace the old send_email_alert function with this one ---
def send_email_alert(subject, body):
    """
    Sends an email using Amazon SES.
    """
    if not FROM_EMAIL or not TO_EMAIL:
        print("Email environment variables not set. Skipping alert.")
        return
        
    print(f"Sending email with subject: '{subject}'")
    try:
        ses_client.send_email(
            Source=FROM_EMAIL,
            Destination={'ToAddresses': [TO_EMAIL]},
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}}
            }
        )
        print("Email alert sent successfully.")
    except Exception as e:
        # This will catch errors, e.g., if the recipient isn't verified and you're in the sandbox
        print(f"Error sending email: {e}")

# --- Main Lambda Handler ---
def daily_reminder_handler(event, context):
    """
    This function runs once a day. It reads the sheet, checks for
    due dates, and sends notifications.
    """
    print("Starting daily reminder check...")
    # Use today's date based on MST for consistency, though Lambda runs on UTC
    today = datetime.now().date()
    
    try:
        # 1. Authenticate and get sheet data
        gc = get_gspread_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        worksheet = spreadsheet.sheet1
        
        # Get all values as a list of lists, which preserves the raw string data
        all_values = worksheet.get_all_values()
        # The first item in the list is the header row
        headers = all_values[0]
        # The rest of the items are the data rows
        reminders_as_lists = all_values[1:]
        print(f"Found {len(reminders_as_lists)} records in the spreadsheet.")

        # Create a mapping of header names to their column index for easy access
        header_map = {header: i for i, header in enumerate(headers)}

        # 2. Loop through each reminder from the sheet
        for i, row in enumerate(reminders_as_lists):
            row_num = i + 2 # +1 for 0-index, +1 for header row
            
            # Use .get(header, '') to safely get data even if column is missing
            item_name = row[header_map.get('ItemName')] if 'ItemName' in header_map and len(row) > header_map.get('ItemName') else ''
            due_date_str = row[header_map.get('DueDate')] if 'DueDate' in header_map and len(row) > header_map.get('DueDate') else ''

            print(f"\n--- Processing Row {row_num}: {item_name} ---")

            if not item_name or not due_date_str:
                print(f"Skipping row {row_num} due to missing ItemName or DueDate.")
                continue
            
            item_id = f"{item_name.replace(' ', '-')}-{due_date_str}"
            print(f"Generated ItemID: {item_id}")

            response = table.get_item(Key={'ItemID': item_id})
            item_status = response.get('Item', {}).get('Status', 'Active')
            print(f"DynamoDB Status: {item_status}")

            if item_status == 'Handled':
                print("Item is handled. Skipping.")
                continue
            
            if 'Item' not in response:
                print(f"New item. Adding to DynamoDB as Active.")
                table.put_item(Item={'ItemID': item_id, 'Status': 'Active'})

            try:
                # We will use the MM/DD/YYYY format as per our sheet's default
                due_date = datetime.strptime(due_date_str, '%m/%d/%Y').date()
                days_until_due = (due_date - today).days
                print(f"Parsed DueDate: {due_date}, Days Until Due: {days_until_due}")
            except ValueError:
                print(f"ERROR: Could not parse date '{due_date_str}'. Ensure format is MM/DD/YYYY.")
                continue

            if days_until_due < 0:
                policy_no = row[header_map.get('Policy/Inv. No.')] if 'Policy/Inv. No.' in header_map and len(row) > header_map.get('Policy/Inv. No.') else 'N/A'
                subject = f"OVERDUE: {item_name}"
                body = f"This item was due on {due_date_str} and is overdue by {-days_until_due} days.\nPolicy No: {policy_no}"
                print(f"OVERDUE MATCH. Sending notification.")
                send_telegram_alert(f"🚨 {subject}\n{body}")
                send_email_alert(subject, body)
                continue
            
            advance_days_str = row[header_map.get('AdvanceDays')] if 'AdvanceDays' in header_map and len(row) > header_map.get('AdvanceDays') else ''
            advance_days_list = [int(d.strip()) for d in advance_days_str.split(',') if d.strip()]
            print(f"Advance Days List to check against: {advance_days_list}")

            if days_until_due in advance_days_list:
                print(f"MATCH FOUND! Sending notification for {days_until_due} days away.")
                policy_no = row[header_map.get('Policy/Inv. No.')] if 'Policy/Inv. No.' in header_map and len(row) > header_map.get('Policy/Inv. No.') else 'N/A'
                amount = row[header_map.get('Amount')] if 'Amount' in header_map and len(row) > header_map.get('Amount') else 'N/A'
                name_on_inv = row[header_map.get('Name on Inv.')] if 'Name on Inv.' in header_map and len(row) > header_map.get('Name on Inv.') else 'N/A'
                place_branch = row[header_map.get('Place/Branch')] if 'Place/Branch' in header_map and len(row) > header_map.get('Place/Branch') else 'N/A'

                subject = f"Reminder: {item_name} in {days_until_due} days"
                body = (f"This is a reminder that your '{item_name}' is due on {due_date_str}.\n\n"
                        f"Policy/Inv. No.: {policy_no}\n"
                        f"Amount: {amount}\n"
                        f"Name on Inv.: {name_on_inv}\n"
                        f"Place/Branch: {place_branch}")
                send_telegram_alert(f"🔔 {subject}")
                send_email_alert(subject, body) # Send the more detailed body to email
            else:
                print(f"No reminder needed today.")
        
        print("\nDaily reminder check finished successfully.")
        return {'statusCode': 200, 'body': json.dumps('Success')}

    except Exception as e:
        print(f"An unhandled error occurred: {e}")
        # Send an error alert to yourself
        send_telegram_alert(f"🔴 CRITICAL ERROR in Lambda: {e}")
        raise e

# --- Second Lambda Handler (Placeholder) ---
def action_handler(event, context):
    """
    This function is triggered by the API Gateway when a user
    clicks a button in Telegram.
    """
    print("Action Handler function executed!")
    # We will build this logic later
    return {'statusCode': 200, 'body': json.dumps('Action received')}