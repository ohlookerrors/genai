import os
import logging
from typing import Optional, Dict
from supabase import create_client, Client

logger = logging.getLogger("avery")

class DatabaseService:
    """Handles all database operations with Supabase"""
    
    def __init__(self):
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")
        
        if not supabase_url or not supabase_key:
            raise Exception("SUPABASE_URL and SUPABASE_KEY must be set")
        
        self.client: Client = create_client(supabase_url, supabase_key)
        logger.info("✓ Database service initialized")
    
    async def get_borrower_by_phone(self, phone_number: str) -> Optional[Dict]:
        """
        Retrieve borrower information by phone number
        
        Args:
            phone_number: Phone number in E.164 format (e.g., +14155551234)
        
        Returns:
            Dictionary with borrower data or None if not found
        """
        try:
            phone_number = phone_number.strip()
            
            response = self.client.table("borrowers").select("*").eq("phone_number", phone_number).execute()
            
            # Handle response
            data = response.data if hasattr(response, 'data') else response.get('data')
            
            if data and len(data) > 0:
                borrower = data[0]
                logger.info(f"✓ Found borrower: {borrower.get('name')} ({phone_number})")
                return borrower
            else:
                logger.warning(f"✗ No borrower found for {phone_number}")
                return None
                
        except Exception as e:
            logger.error(f"✗ Database error: {e}")
            raise
    
    async def update_borrower_status(self, phone_number: str, updates: Dict) -> bool:
        """
        Update borrower record
        
        Args:
            phone_number: Phone number to identify borrower
            updates: Dictionary of fields to update
        
        Returns:
            True if successful
        """
        try:
            response = self.client.table("borrowers").update(updates).eq("phone_number", phone_number).execute()
            
            logger.info(f"✓ Updated borrower {phone_number}: {updates}")
            return True
            
        except Exception as e:
            logger.error(f"✗ Update error: {e}")
            return False
    
    async def log_call_event(self, call_data: Dict) -> bool:
        """
        Log call event to database (if you have a calls table)
        
        Args:
            call_data: Dictionary with call information
        
        Returns:
            True if successful
        """
        try:
            # You can create a 'calls' table to track call history
            # For now, just update the borrower's last_call_status
            
            phone_number = call_data.get("phone_number")
            if phone_number:
                await self.update_borrower_status(
                    phone_number,
                    {
                        "last_call_status": call_data.get("status", "completed"),
                        "updated_at": call_data.get("timestamp")
                    }
                )
            
            return True
            
        except Exception as e:
            logger.error(f"✗ Call logging error: {e}")
            return False