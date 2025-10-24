import os
import json
import base64
import asyncio
from datetime import datetime
from typing import Optional, Dict
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, status, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, validator
import websockets
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv

from app.services.rag_service import RAGService
from app.services.db_service import DatabaseService

load_dotenv()

logger = logging.getLogger("avery")

# Initialize router
voice_router = APIRouter(prefix="", tags=["voice"])

# Environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SERVER_URL = os.getenv("SERVER_URL")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

if not SERVER_URL:
    raise Exception("SERVER_URL must be set in environment")

# Initialize clients
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
rag_service = RAGService()
db_service = DatabaseService()

# Active calls tracking
active_calls: Dict[str, dict] = {}

# -------------------------
# Pydantic Models
# -------------------------

class MakeCallRequest(BaseModel):
    """Request model for initiating outbound calls"""
    to_number: str = Field(
        ..., 
        description="Phone number to call in E.164 format (e.g., +14155551234)",
        example="+14155551234"
    )
    custom_greeting: Optional[str] = Field(
        None,
        description="Optional custom greeting"
    )
    

# -------------------------
# Deepgram Configuration
# -------------------------

async def connect_to_deepgram():
    """Connect to Deepgram Voice Agent"""
    deepgram_url = "wss://agent.deepgram.com/v1/agent/converse"
    
    logger.info("🔌 Connecting to Deepgram...")
    try:
        connection = await websockets.connect(
            deepgram_url,
            subprotocols=["token", DEEPGRAM_API_KEY],
            ping_interval=20,
            ping_timeout=10
        )
        logger.info("✅ Connected to Deepgram!")
        return connection
    except Exception as e:
        logger.error(f"❌ Deepgram connection failed: {e}")
        raise

def get_agent_config() -> dict:
    """
    Deepgram Voice Agent Configuration
    PASSIVE MODE: Agent is completely silent, only handles STT/TTS
    All conversation logic handled by RAG service
    """
    return {
        "type": "Settings",
        "audio": {
            "input": {"encoding": "mulaw", "sample_rate": 8000},
            "output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"}
        },
        "agent": {
            "language": "en",
            "listen": {
                "provider": {"type": "deepgram", "model": "nova-3"}
            },
            "think": {
                "provider": {"type": "open_ai", "model": "gpt-4o-mini", "temperature": 0.0},
                "prompt": """You are a SILENT proxy agent. You have NO conversational role.

CRITICAL RULES:
1. NEVER generate your own responses
2. NEVER speak unless explicitly told via InjectAgentMessage
3. You are ONLY a conduit for external system responses
4. Your ONLY job is to wait for InjectAgentMessage commands
5. Do NOT acknowledge, confirm, or respond to anything the user says
6. Do NOT provide helpful responses, suggestions, or clarifications
7. Remain completely SILENT until injected message arrives

When you receive InjectAgentMessage, speak EXACTLY what is provided - nothing more, nothing less."""
            },
            "speak": {
                "provider": {"type": "deepgram", "model": "aura-2-andromeda-en"}
            },
            "greeting": "Hi, this is Avery, a virtual agent with Essex Mortgage calling on a recorded line."
        }
    }
# -------------------------
# Twilio Endpoints
# -------------------------

@voice_router.post("/makecall")
async def make_call(request: MakeCallRequest):
    """Initiate outbound call"""
    try:
        to_number = request.to_number
        
        logger.info(f"📞 Initiating call to {to_number}")
        
        call = twilio_client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/twiml",
            status_callback=f"{SERVER_URL}/call-status",
            status_callback_event=["initiated", "ringing", "answered", "completed"]
        )
        
        # Store phone mapping
        active_calls[call.sid] = {
            "call_sid": call.sid,
            "to_number": to_number,
            "from_number": TWILIO_PHONE_NUMBER,
            "started_at": datetime.utcnow().isoformat()
        }
        
        logger.info(f"✅ Call initiated: {call.sid}")
        logger.info(f"📱 Stored mapping: {call.sid} → {to_number}")
        
        return {
            "status": "success",
            "call_sid": call.sid,
            "to": to_number,
            "from": TWILIO_PHONE_NUMBER,
            "message": "Call initiated successfully"
        }
        
    except Exception as e:
        logger.error(f"❌ Error making call: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@voice_router.api_route("/twiml", methods=["GET", "POST"])
async def generate_twiml():
    """Generate TwiML for Twilio"""
    response = VoiceResponse()
    connect = Connect()
    
    websocket_url = SERVER_URL.replace('https://', 'wss://').replace('http://', 'ws://')
    websocket_url = f"{websocket_url}/twilio"
    
    stream = Stream(url=websocket_url)
    connect.append(stream)
    response.append(connect)
    
    logger.info(f"✅ Generated TwiML")
    
    return Response(content=str(response), media_type="application/xml")

@voice_router.post("/call-status")
async def call_status_webhook(request: Request):
    """Handle Twilio call status updates"""
    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
        call_status = form_data.get("CallStatus")
        from_number = form_data.get("From")
        to_number = form_data.get("To")
        
        logger.info(f"📊 Call status: {call_sid} → {call_status}")
        logger.info(f"📱 From: {from_number}, To: {to_number}")
        
        return {"status": "received"}
    except Exception as e:
        logger.error(f"❌ Error in call-status: {e}")
        return {"status": "error", "message": str(e)}

# -------------------------
# Main WebSocket Handler
# -------------------------

@voice_router.websocket("/twilio")
async def handle_twilio_call(websocket: WebSocket):
    """
    Main WebSocket handler
    Bridges Twilio ↔ Deepgram and integrates RAG for business logic
    """
    await websocket.accept()
    logger.info("📞 Twilio WebSocket connected")
    
    # State variables
    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None
    from_number: Optional[str] = None
    deepgram_ws = None
    
    # Communication queues
    audio_queue = asyncio.Queue()
    stream_sid_queue = asyncio.Queue()
    
    try:
        # Connect to Deepgram
        deepgram_ws = await connect_to_deepgram()
        
        # Send configuration
        agent_config = get_agent_config()
        await deepgram_ws.send(json.dumps(agent_config))
        logger.info("⚙️ Deepgram configured with SILENT mode")
        
        # Task 1: Receive from Twilio
        async def twilio_receiver():
            """Receives audio from Twilio"""
            nonlocal stream_sid, call_sid, from_number
            audio_buffer = bytearray()
            BUFFER_SIZE = 8000
            
            try:
                while True:
                    message = await websocket.receive_text()
                    data = json.loads(message)
                    
                    if data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        call_sid = data["start"]["callSid"]
                        
                        # Get phone from stored mapping
                        existing_call = active_calls.get(call_sid)
                        if existing_call:
                            from_number = existing_call.get("to_number")
                            logger.info(f"✅ Found phone: {from_number}")
                        else:
                            from_number = call_sid
                            logger.warning(f"⚠️ No stored phone for {call_sid}")
                        
                        logger.info(f"📞 Call started: {stream_sid}")
                        logger.info(f"📱 Borrower: {from_number}")
                        
                        # Clear any old session
                        rag_service.clear_session(from_number)
                        logger.info(f"🔄 Cleared session - fresh start")
                        
                        # Store with stream_sid
                        active_calls[stream_sid] = {
                            "call_sid": call_sid,
                            "from_number": from_number,
                            "started_at": datetime.utcnow().isoformat()
                        }
                        
                        await stream_sid_queue.put(stream_sid)
                        
                    elif data["event"] == "media":
                        audio_payload = data["media"]["payload"]
                        chunk = base64.b64decode(audio_payload)
                        audio_buffer.extend(chunk)
                        
                        if len(audio_buffer) >= BUFFER_SIZE:
                            await audio_queue.put(bytes(audio_buffer))
                            audio_buffer.clear()
                            
                    elif data["event"] == "stop":
                        logger.info("📞 Call ended by Twilio")
                        break
                        
            except WebSocketDisconnect:
                logger.info("🔌 Twilio disconnected")
            except Exception as e:
                logger.error(f"❌ Error in twilio_receiver: {e}")
        
        # Task 2: Send to Deepgram
        async def deepgram_sender():
            """Sends audio to Deepgram"""
            try:
                while True:
                    audio_chunk = await audio_queue.get()
                    if deepgram_ws:
                        await deepgram_ws.send(audio_chunk)
            except Exception as e:
                logger.error(f"❌ Error in deepgram_sender: {e}")
        
        # Task 3: Receive from Deepgram & Inject RAG responses
        async def deepgram_receiver():
            """Receives from Deepgram, processes through RAG"""
            sid = await stream_sid_queue.get()
            logger.info("✅ Ready for processing")
            
            # Send initial greeting immediately after connection
            call_info = active_calls.get(sid, {})
            phone = call_info.get("from_number")
            
            if phone:
                # Trigger initial greeting with empty text
                try:
                    logger.info("🎙️ Sending initial greeting...")
                    rag_result = await rag_service.process_utterance(phone, "")
                    initial_greeting = rag_result.get("response", "")
                    
                    if initial_greeting:
                        inject_msg = {
                            "type": "InjectAgentMessage",
                            "message": initial_greeting
                        }
                        await deepgram_ws.send(json.dumps(inject_msg))
                        logger.info(f"📤 Initial greeting sent: {initial_greeting[:50]}...")
                except Exception as e:
                    logger.error(f"❌ Error sending initial greeting: {e}")
            
            try:
                async for message in deepgram_ws:
                    if isinstance(message, str):
                        data = json.loads(message)
                        msg_type = data.get("type")
                        
                        # Handle barge-in
                        if msg_type == "UserStartedSpeaking":
                            logger.info("🎤 User started speaking - clearing queue")
                            await websocket.send_json({
                                "event": "clear",
                                "streamSid": sid
                            })
                        
                        # Handle conversation
                        elif msg_type == "ConversationText":
                            role = data.get("role", "")
                            content = data.get("content", "")
                            
                            # Log non-empty, non-system messages
                            if content and content != "InjectAgentMessage" and role in ["user", "assistant"]:
                                logger.info(f"💬 {role}: {content}")
                            
                            # Process ONLY user speech through RAG
                            if role == "user" and content.strip():
                                call_info = active_calls.get(sid, {})
                                phone = call_info.get("from_number")
                                
                                if not phone:
                                    logger.error("❌ No phone number for call!")
                                    continue
                                
                                # Check if already in transfer state
                                session = rag_service._get_session(phone)
                                if session.get("stage") == "transfer":
                                    logger.info("🚫 Already in transfer - ignoring user input")
                                    continue
                                
                                logger.info(f"🧠 Processing through RAG: '{content}'")
                                
                                try:
                                    # Process through RAG
                                    rag_result = await rag_service.process_utterance(phone, content)
                                    
                                    agent_response = rag_result.get("response", "")
                                    should_transfer = rag_result.get("transfer", False)
                                    
                                    # Inject response if not empty
                                    if agent_response.strip():
                                        logger.info(f"✅ RAG Response: '{agent_response[:60]}...'")
                                        
                                        inject_msg = {
                                            "type": "InjectAgentMessage",
                                            "message": agent_response
                                        }
                                        
                                        await deepgram_ws.send(json.dumps(inject_msg))
                                        logger.info("📤 Response injected to Deepgram")
                                    
                                    # Handle transfer
                                    if should_transfer:
                                        logger.info("🔄 Transfer requested by RAG")
                                        logger.info("ℹ️ Transfer to Level 2 agent would happen here")
                                        # Stop processing to avoid repeated messages
                                        break
                                    
                                except Exception as e:
                                    logger.error(f"❌ RAG processing error: {e}", exc_info=True)
                    
                    # Binary audio from Deepgram (TTS output)
                    elif isinstance(message, bytes):
                        audio_b64 = base64.b64encode(message).decode("utf-8")
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": sid,
                            "media": {"payload": audio_b64}
                        })
                        
            except websockets.exceptions.ConnectionClosed:
                logger.info("🔌 Deepgram connection closed")
            except Exception as e:
                logger.error(f"❌ Error in deepgram_receiver: {e}", exc_info=True)
        
        # Run all tasks concurrently
        await asyncio.gather(
            twilio_receiver(),
            deepgram_sender(),
            deepgram_receiver(),
            return_exceptions=True
        )
        
    except Exception as e:
        logger.error(f"❌ Error handling call: {e}", exc_info=True)
        
    finally:
        # Cleanup
        if deepgram_ws:
            await deepgram_ws.close()
            logger.info("🔌 Deepgram closed")
        
        if stream_sid and stream_sid in active_calls:
            call_info = active_calls.get(stream_sid, {})
            phone = call_info.get("from_number")
            if phone:
                rag_service.clear_session(phone)
                logger.info(f"🗑️ Cleared session for {phone}")
            
            del active_calls[stream_sid]
            logger.info(f"🗑️ Removed call {stream_sid}")