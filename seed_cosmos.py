import json
import os
from azure.cosmos import CosmosClient, PartitionKey
from dotenv import load_dotenv

load_dotenv()

COSMOS_CONNECTION_STRING = os.environ["COSMOS_CONNECTION_STRING"]
DATABASE_NAME = os.environ.get("COSMOS_DB_NAME", "cold_caller_db")

client = CosmosClient.from_connection_string(COSMOS_CONNECTION_STRING)
database = client.get_database_client(DATABASE_NAME)

def seed_container(container_name, json_file_path):
    container = database.get_container_client(container_name)
    
    with open(json_file_path, 'r') as f:
        items = json.load(f)
    
    for item in items:
        try:
            container.upsert_item(item)
            print(f"  ✓ {item['id']}")
        except Exception as e:
            print(f"  ✗ {item['id']}: {e}")

print("Seeding knowledge-base...")
seed_container("knowledge-base", "seed_data/knowledge_base.json")

print("\nSeeding leads...")
seed_container("leads", "seed_data/leads.json")

print("\nSeeding agent-config...")
seed_container("agent-config", "seed_data/agent_config.json")

print("\nDone!")