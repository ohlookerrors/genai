import os
import logging
from typing import Dict, Optional
from datetime import datetime
from openai import AzureOpenAI

from app.services.db_service import DatabaseService

logger = logging.getLogger("avery")

class RAGService:
    """Handles complete conversation flow and verification logic"""
    
    def __init__(self):
        self.db = DatabaseService()
        
        # Initialize Azure OpenAI client (for future RAG use)
        self.client = AzureOpenAI(
            api_key=os.getenv("AZURE_OPENAI_KEY"),
            api_version="2024-05-01-preview",
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
        )
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        
        # Session state: phone_number -> state dict
        self.sessions: Dict[str, dict] = {}
    
    def _get_session(self, phone_number: str) -> dict:
        """Get or create session state"""
        if phone_number not in self.sessions:
            self.sessions[phone_number] = {
                "stage": "initial_greeting",
                "attempts": 0,
                "verified_dob": False,
                "verified_account": False,
                "verified_address": False,
                "conversation_history": [],
                "partial_dob": "",
                "name_confirmed": False
            }
            logger.info(f"🆕 Created new session for {phone_number}")
        return self.sessions[phone_number]
    
    async def process_utterance(self, phone_number: str, user_text: str) -> dict:
        """
        Process user utterance and return agent response
        
        Returns:
        {
            "response": "text for agent to speak",
            "stage": "current verification stage",
            "transfer": boolean indicating if transfer needed
        }
        """
        try:
            # Normalize inputs
            phone_number = phone_number.strip()
            user_text = user_text.strip()
            
            logger.info(f"📥 RAG Input: phone={phone_number}, text='{user_text}'")
            
            # Get session FIRST to check transfer state
            session = self._get_session(phone_number)
            
            # CRITICAL: Block processing if already in transfer
            if session["stage"] == "transfer":
                logger.info("🔄 Already in transfer stage - blocking further processing")
                return {
                    "response": "",  # Empty to avoid repetition
                    "stage": "transfer",
                    "transfer": True
                }
            
            # Get borrower data
            borrower = await self.db.get_borrower_by_phone(phone_number)
            if not borrower:
                logger.warning(f"✗ No borrower found for {phone_number}")
                session["stage"] = "transfer"
                return {
                    "response": "I'm sorry, I couldn't locate your account in our system. Let me connect you to a representative who can assist you. Please hold.",
                    "stage": "no_record",
                    "transfer": True
                }
            
            logger.info(f"✓ Found borrower: {borrower.get('name')} ({phone_number})")
            
            # Add to conversation history
            if user_text:  # Only add non-empty user messages
                session["conversation_history"].append({
                    "role": "user",
                    "content": user_text,
                    "timestamp": datetime.utcnow().isoformat()
                })
            
            current_stage = session["stage"]
            logger.info(f"📍 Current stage: {current_stage}")
            
            # Route based on stage
            if current_stage == "initial_greeting":
                result = self._handle_initial_greeting(session, borrower)
            elif current_stage == "confirm_identity":
                result = self._handle_confirm_identity(session, borrower, user_text)
            elif current_stage == "verify_dob":
                result = self._handle_verify_dob(session, borrower, user_text)
            elif current_stage == "verify_account":
                result = self._handle_verify_account(session, borrower, user_text)
            elif current_stage == "verify_address":
                result = self._handle_verify_address(session, borrower, user_text)
            elif current_stage == "verification_complete":
                result = self._handle_verification_complete(session, borrower)
            elif current_stage == "payment_discussion":
                result = await self._handle_payment_discussion(session, borrower, user_text)
            else:
                result = {
                    "response": "I'm sorry, I didn't catch that. Could you please repeat?",
                    "stage": current_stage,
                    "transfer": False
                }
            
            logger.info(f"📤 RAG Output: stage={result['stage']}, transfer={result['transfer']}")
            return result
                
        except Exception as e:
            logger.error(f"❌ Error processing utterance: {e}", exc_info=True)
            return {
                "response": "I'm experiencing technical difficulties. Let me connect you to a representative who can help.",
                "stage": "error",
                "transfer": True
            }
    
    def _handle_initial_greeting(self, session: dict, borrower: dict) -> dict:
        """Initial greeting - agent introduces itself"""
        session["stage"] = "confirm_identity"
        session["attempts"] = 0
        
        name = borrower.get("name", "the account holder")
        
        response = (
            f"Hi, this is Avery calling from Essex Mortgage on a recorded line. "
            f"May I please speak with {name}?"
        )
        
        logger.info(f"✅ Initial greeting → Moving to confirm_identity")
        
        return {
            "response": response,
            "stage": session["stage"],
            "transfer": False
        }
    
    def _handle_confirm_identity(self, session: dict, borrower: dict, user_text: str) -> dict:
        """Confirm we're speaking to the right person"""
        user_lower = user_text.lower()
        
        # Check for affirmative responses
        affirmative = any(word in user_lower for word in [
            "yes", "yeah", "yep", "sure", "speaking", "this is", "correct", 
            "that's me", "thats me", "i am", "im", "yup", "uh huh"
        ])
        
        # Check for negative responses
        negative = any(word in user_lower for word in [
            "no", "nope", "not", "wrong", "different", "isn't"
        ])
        
        if affirmative:
            session["name_confirmed"] = True
            session["stage"] = "verify_dob"
            session["attempts"] = 0
            
            logger.info("✅ Identity confirmed → Moving to verify_dob")
            
            return {
                "response": "Thank you for confirming. For security purposes, I need to verify a few details. Could you please provide your full date of birth?",
                "stage": session["stage"],
                "transfer": False
            }
        
        elif negative:
            session["stage"] = "transfer"
            logger.info("❌ Wrong person → Transfer")
            
            return {
                "response": "I apologize for the confusion. Let me connect you to a representative who can assist you properly. Please hold.",
                "stage": session["stage"],
                "transfer": True
            }
        
        else:
            # Unclear response, ask again
            session["attempts"] += 1
            
            if session["attempts"] >= 2:
                session["stage"] = "transfer"
                return {
                    "response": "I'm having difficulty confirming your identity. Let me transfer you to a representative. Please hold.",
                    "stage": session["stage"],
                    "transfer": True
                }
            
            name = borrower.get("name", "the account holder")
            return {
                "response": f"I'm sorry, I didn't catch that. Am I speaking with {name}? Please say yes or no.",
                "stage": session["stage"],
                "transfer": False
            }
    
    def _handle_verify_dob(self, session: dict, borrower: dict, user_text: str) -> dict:
        """Verify date of birth with partial input support"""
        dob_db = borrower.get("date_of_birth")
        verified = False
        
        logger.info(f"🔍 Verifying DOB: user said '{user_text}', DB has '{dob_db}'")
        
        # Initialize partial date storage
        if "partial_dob" not in session:
            session["partial_dob"] = ""
        
        # Comprehensive word-to-number mapping
        word_to_num = [
            # Months first (longest matches)
            ("september", "09"), ("november", "11"), ("december", "12"),
            ("february", "02"), ("january", "01"), ("october", "10"),
            ("august", "08"), ("april", "04"), ("march", "03"),
            ("june", "06"), ("july", "07"), ("sept", "09"),
            ("may", "05"), ("jan", "01"), ("feb", "02"), 
            ("mar", "03"), ("apr", "04"), ("jun", "06"),
            ("jul", "07"), ("aug", "08"), ("sep", "09"),
            ("oct", "10"), ("nov", "11"), ("dec", "12"),
            # Teens (before single digits)
            ("thirteen", "13"), ("fourteen", "14"), ("fifteen", "15"),
            ("sixteen", "16"), ("seventeen", "17"), ("eighteen", "18"),
            ("nineteen", "19"), ("eleventh", "11"), ("twelfth", "12"),
            ("thirteenth", "13"), ("fourteenth", "14"), ("fifteenth", "15"),
            ("sixteenth", "16"), ("seventeenth", "17"), ("eighteenth", "18"),
            ("nineteenth", "19"),
            # Tens
            ("twenty", "20"), ("thirty", "30"), ("forty", "40"),
            ("fifty", "50"), ("sixty", "60"), ("seventy", "70"),
            ("eighty", "80"), ("ninety", "90"),
            ("twentieth", "20"), ("thirtieth", "30"), ("thirty first", "31"),
            # Tens + ordinals
            ("tenth", "10"), ("eleven", "11"), ("twelve", "12"),
            # Single digits last
            ("zero", "0"), ("one", "1"), ("two", "2"), ("three", "3"),
            ("four", "4"), ("five", "5"), ("six", "6"), ("seven", "7"),
            ("eight", "8"), ("nine", "9"), ("ten", "10"),
            ("first", "1"), ("second", "2"), ("third", "3"),
            ("fourth", "4"), ("fifth", "5"), ("sixth", "6"),
            ("seventh", "7"), ("eighth", "8"), ("ninth", "9"),
        ]
        
        # Normalize user text
        user_normalized = user_text.lower()
        for word, num in word_to_num:
            user_normalized = user_normalized.replace(word, num)
        
        # Extract digits from current input
        current_digits = ''.join(c for c in user_normalized if c.isdigit())
        
        # Accumulate partial input
        session["partial_dob"] += current_digits
        accumulated_digits = session["partial_dob"]
        
        logger.info(f"🔢 Current digits: '{current_digits}'")
        logger.info(f"🔢 Accumulated: '{accumulated_digits}'")
        
        # Verify against database
        if dob_db:
            if isinstance(dob_db, str):
                try:
                    from datetime import datetime as dt
                    dob_obj = dt.fromisoformat(dob_db).date()
                except:
                    dob_obj = None
            else:
                dob_obj = dob_db
            
            if dob_obj:
                # All possible date formats
                formats = [
                    dob_obj.strftime("%m%d%Y"),      # 09141986
                    dob_obj.strftime("%d%m%Y"),      # 14091986
                    dob_obj.strftime("%Y%m%d"),      # 19860914
                    dob_obj.strftime("%m%d%y"),      # 091486
                    dob_obj.strftime("%d%m%y"),      # 140986
                ]
                
                logger.info(f"🔍 Checking: '{accumulated_digits}' against {formats}")
                
                # Check accumulated digits
                for fmt in formats:
                    if fmt == accumulated_digits or (len(accumulated_digits) >= 6 and fmt in accumulated_digits):
                        verified = True
                        logger.info(f"✅ DOB MATCHED: {fmt}")
                        break
                
                # Also check current input for full dates
                if not verified:
                    for fmt in formats:
                        if fmt == current_digits:
                            verified = True
                            logger.info(f"✅ DOB MATCHED: {fmt}")
                            break
        
        if verified:
            session["verified_dob"] = True
            session["stage"] = "verify_account"
            session["attempts"] = 0
            session["partial_dob"] = ""
            
            logger.info("✅ DOB verified → Moving to verify_account")
            
            return {
                "response": "Thank you. Now, could you please tell me the last four digits of your bank account number on file?",
                "stage": session["stage"],
                "transfer": False
            }
        else:
            # Determine if we need more input or failed verification
            if len(accumulated_digits) >= 8:
                # Enough digits provided, but wrong
                session["attempts"] += 1
                session["partial_dob"] = ""  # Reset
                logger.warning(f"❌ DOB verification failed (attempt {session['attempts']}/3)")
                
                if session["attempts"] >= 3:
                    session["stage"] = "transfer"
                    return {
                        "response": "I'm sorry, I couldn't verify your date of birth after three attempts. For your security, I'll connect you to a representative who can assist you. Please hold.",
                        "stage": session["stage"],
                        "transfer": True
                    }
                else:
                    return {
                        "response": f"That doesn't match our records. This is attempt {session['attempts']} of 3. Please provide your complete date of birth, including month, day, and year.",
                        "stage": session["stage"],
                        "transfer": False
                    }
            else:
                # Not enough digits yet
                logger.info(f"ℹ️ Partial DOB: {len(accumulated_digits)} digits collected")
                return {
                    "response": "I've got that. Please continue with the rest of your date of birth.",
                    "stage": session["stage"],
                    "transfer": False
                }
    
    def _handle_verify_account(self, session: dict, borrower: dict, user_text: str) -> dict:
        """Verify last 4 digits of account"""
        account_4 = borrower.get("account_number_last_four", "")
        
        logger.info(f"🔍 Verifying Account: user said '{user_text}', DB has '{account_4}'")
        
        # Word-to-number conversion
        word_to_num = {
            "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
            "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
            "oh": "0"  # People often say "oh" instead of "zero"
        }
        
        text_lower = user_text.lower()
        for word, num in word_to_num.items():
            text_lower = text_lower.replace(word, num)
        
        # Extract digits
        extracted_digits = ''.join(c for c in text_lower if c.isdigit())
        
        logger.info(f"🔢 Extracted: '{extracted_digits}'")
        
        # Verify - check if account_4 matches exactly or is contained
        verified = (account_4 == extracted_digits or 
                   (len(extracted_digits) >= 4 and account_4 in extracted_digits))
        
        if verified:
            session["verified_account"] = True
            session["stage"] = "verify_address"
            session["attempts"] = 0
            
            logger.info("✅ Account verified → Moving to verify_address")
            
            return {
                "response": "Perfect. Lastly, can you confirm the street number or first word of your property address on file?",
                "stage": session["stage"],
                "transfer": False
            }
        else:
            session["attempts"] += 1
            logger.warning(f"❌ Account verification failed (attempt {session['attempts']}/3)")
            
            if session["attempts"] >= 3:
                session["stage"] = "transfer"
                return {
                    "response": "I'm sorry, I couldn't verify your account number. Let me connect you to a representative for further assistance. Please hold.",
                    "stage": session["stage"],
                    "transfer": True
                }
            else:
                return {
                    "response": "I didn't catch that correctly. Could you please repeat the last four digits of your bank account number?",
                    "stage": session["stage"],
                    "transfer": False
                }
    
    def _handle_verify_address(self, session: dict, borrower: dict, user_text: str) -> dict:
        """Verify property address"""
        address = borrower.get("property_address", "")
        
        logger.info(f"🔍 Verifying Address: user said '{user_text}', DB has '{address}'")
        
        verified = False
        if address:
            address_lower = address.lower()
            user_lower = user_text.lower()
            
            # Extract first word and street number from database
            address_parts = address_lower.split()
            first_word = address_parts[0] if address_parts else ""
            street_number = ''.join(c for c in address_parts[0] if c.isdigit()) if address_parts else ""
            
            # Check first word match
            if first_word and first_word in user_lower:
                verified = True
                logger.info(f"✅ Address matched on first word: {first_word}")
            
            # Check street number
            user_digits = ''.join(c for c in user_text if c.isdigit())
            if street_number and user_digits and street_number == user_digits:
                verified = True
                logger.info(f"✅ Address matched on street number: {street_number}")
        
        if verified:
            session["verified_address"] = True
            session["stage"] = "verification_complete"
            session["attempts"] = 0
            
            logger.info("✅ Address verified → Moving to verification_complete")
            
            # Move to verification complete (next step will explain reason for call)
            return self._handle_verification_complete(session, borrower)
        else:
            session["attempts"] += 1
            logger.warning(f"❌ Address verification failed (attempt {session['attempts']}/3)")
            
            if session["attempts"] >= 3:
                session["stage"] = "transfer"
                return {
                    "response": "I'm sorry, I couldn't verify your address. For your security, I'll connect you to a representative. Please hold.",
                    "stage": session["stage"],
                    "transfer": True
                }
            else:
                return {
                    "response": "That doesn't match our records. Could you please confirm the street number or first word of your property address?",
                    "stage": session["stage"],
                    "transfer": False
                }
    
    def _handle_verification_complete(self, session: dict, borrower: dict) -> dict:
        """Verification complete - explain reason for call"""
        session["stage"] = "payment_discussion"
        
        name = borrower.get("name", "").split()[0]  # First name only
        due_amount = borrower.get("due_amount", 0)
        due_date = borrower.get("due_date", "")
        
        # Format due date nicely
        if due_date:
            try:
                from datetime import datetime as dt
                due_date_obj = dt.fromisoformat(str(due_date))
                due_date_formatted = due_date_obj.strftime("%B %d, %Y")
            except:
                due_date_formatted = due_date
        else:
            due_date_formatted = "unknown"
        
        logger.info("✅ Verification complete → Explaining reason for call")
        
        response = (
            f"Thank you for verifying your information, {name}. "
            f"I'm calling today regarding your mortgage account with Essex Mortgage. "
            f"Our records show you have an outstanding balance of ${due_amount:.2f} "
            f"with a due date of {due_date_formatted}. "
            f"Would you like to make a payment today, or do you have any questions about your account?"
        )
        
        return {
            "response": response,
            "stage": session["stage"],
            "transfer": False
        }
    
    async def _handle_payment_discussion(self, session: dict, borrower: dict, user_text: str) -> dict:
        """Handle post-verification conversation about payment"""
        text_lower = user_text.lower()
        
        logger.info(f"💬 Payment discussion: '{user_text}'")
        
        # Payment already made claim
        if any(phrase in text_lower for phrase in [
            "already paid", "paid already", "i paid", "payment made", 
            "sent payment", "made payment", "paid it", "sent it"
        ]):
            payment_status = borrower.get("payment_status", "")
            
            if payment_status == "paid":
                return {
                    "response": "Thank you for confirming. Our records show your payment has been received and processed. Would you like me to send you a confirmation email?",
                    "stage": session["stage"],
                    "transfer": False
                }
            else:
                session["stage"] = "transfer"
                return {
                    "response": "I see. Our current records don't show a recent payment, but it may still be processing. Let me connect you to a payment specialist who can verify this for you immediately. Please hold.",
                    "stage": session["stage"],
                    "transfer": True
                }
        
        # Payment intent - wants to pay
        if any(phrase in text_lower for phrase in [
            "make a payment", "pay now", "pay today", "i'll pay", 
            "want to pay", "would like to pay", "yes", "sure", "okay"
        ]) and not any(phrase in text_lower for phrase in ["can't", "cannot", "won't", "wont"]):
            return {
                "response": "Excellent. I can help you with that. We accept payment by bank draft, debit card, or credit card. Which payment method would you prefer today?",
                "stage": session["stage"],
                "transfer": False
            }
        
        # Hardship/assistance request
        if any(phrase in text_lower for phrase in [
            "hardship", "financial", "assistance", "help", "can't pay", 
            "cannot pay", "difficult", "struggling", "program", "trouble"
        ]):
            if borrower.get("hardship_eligible"):
                session["stage"] = "transfer"
                return {
                    "response": "I understand, and I'm here to help. Based on your account, you may qualify for one of our assistance programs. Let me connect you with a hardship specialist who can discuss your options. Please hold.",
                    "stage": session["stage"],
                    "transfer": True
                }
            else:
                session["stage"] = "transfer"
                return {
                    "response": "I understand your situation. Let me connect you with a specialist who can review your account and discuss what options might be available to you. Please hold.",
                    "stage": session["stage"],
                    "transfer": True
                }
        
        # Questions about account/balance
        if any(phrase in text_lower for phrase in [
            "question", "explain", "why", "how much", "balance", 
            "what is", "tell me", "confused", "don't understand", "dont understand"
        ]):
            return {
                "response": "I'd be happy to help answer your questions. What specifically would you like to know about your account or balance?",
                "stage": session["stage"],
                "transfer": False
            }
        
        # Transfer request
        if any(phrase in text_lower for phrase in [
            "transfer", "representative", "speak to someone", 
            "talk to", "human", "person", "someone else"
        ]):
            session["stage"] = "transfer"
            return {
                "response": "Of course. Let me connect you to a representative right away. Please hold.",
                "stage": session["stage"],
                "transfer": True
            }
        
        # Negative/Not interested
        if any(phrase in text_lower for phrase in [
            "no", "not now", "not interested", "later", "can't talk", 
            "busy", "not good time", "call back"
        ]):
            return {
                "response": "I understand. Is there a better time for us to reach you, or would you prefer to call us back at your convenience? Our customer service line is available Monday through Friday, 9 AM to 5 PM.",
                "stage": session["stage"],
                "transfer": False
            }
        
        # Default - unclear response
        return {
            "response": "I want to make sure I help you properly. Are you looking to make a payment today, do you have questions about your account, or would you like to speak with a specialist?",
            "stage": session["stage"],
            "transfer": False
        }
    
    def clear_session(self, phone_number: str):
        """Clear session state for a phone number"""
        if phone_number in self.sessions:
            del self.sessions[phone_number]
            logger.info(f"🗑️ Cleared session for {phone_number}")