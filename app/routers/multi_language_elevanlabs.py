from fastapi import WebSocket, APIRouter, WebSocketDisconnect, HTTPException, status
import websockets
from app.config import logger
import os
import asyncio
from typing import Optional, Dict
import json
from datetime import datetime
import base64
from dotenv import load_dotenv
load_dotenv()

multi_router = APIRouter()

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from fastapi.responses import Response

# Twilio environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SERVER_URL = os.getenv("SERVER_URL")

# Twilio client
twillio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

@multi_router.post("/makecall")
async def make_call(to_number: str, custom_greeting: str):
    try:
        if not to_number.startswith("+"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid phone number format. Must start with '+' and country code."
            )
        
        logger.info(f"Initiating outbound call to {to_number}")

        # Making the outbound call with Twilio
        call = twillio_client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/twilml",
            status_callback=f"{SERVER_URL}/call_status",  # ✅ Fixed endpoint name
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

        logger.info(f"Call initiated with SID: {call.sid}")

        return {
            "status": "success",
            "call_sid": call.sid,
            "to": to_number,
            "from": TWILIO_PHONE_NUMBER,
            "message": "Outbound call initiated successfully"
        }
    except Exception as e:
        logger.error(f"Error making call: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

# TwiML endpoint to handle the call and connect to deepgram via websocket
@multi_router.api_route("/twilml", methods=["GET", "POST"])
async def generate_twiml():
    response = VoiceResponse()

    # Create <Connect> verb with <Stream>
    connect = Connect()
    # NOTE: Use wss:// for secure WebSocket (https), ws:// for http
    websocket_url = f"{SERVER_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/twilio"

    stream = Stream(url=websocket_url)

    connect.append(stream)
    response.append(connect)

    logger.info(f"Generated TwiML for outbound call")

    return Response(content=response.to_xml(), media_type="application/xml")

# ✅ FIXED: Changed from /call-status to /call_status (matches Twilio callback)
@multi_router.post("/call_status")
async def call_status_webhook(
    CallSid: str,
    CallStatus: str,
    From: Optional[str] = None,
    To: Optional[str] = None
):
    """
    Track call status changes from Twilio:
    - initiated: Call is starting
    - ringing: Phone is ringing
    - in-progress: Call answered
    - completed: Call ended
    """
    logger.info(f"Call status update - SID: {CallSid}, Status: {CallStatus}")
    
    return {"status": "received"}

# Deepgram API key
api_key = os.getenv("DEEPGRAM_API_KEY")

# Store active calls (for monitoring)
active_calls: Dict[str, dict] = {}

# ✅ Voice mappings for language switching
DEEPGRAM_VOICES = {
    "en": "aura-2-thalia-en",  # English female voice (your original choice)
    "es": "aura-2-celeste-es"      # Spanish female voice
}

async def connect_to_deepgram():
    """
    Establish WebSocket connection to Deepgram Voice Agent API.
    
    Returns:
        WebSocket connection to Deepgram Voice Agent
    """
    deepgram_url = "wss://agent.deepgram.com/v1/agent/converse"  # ✅ Updated to correct endpoint

    logger.info("🔌 Connecting to Deepgram Voice Agent...")
    try:
        connection = await websockets.connect(
            deepgram_url,
            subprotocols=["token", api_key],
            ping_interval=20,
            ping_timeout=10
        )
        
        logger.info("✅ Connected to Deepgram Voice Agent!")
        return connection
        
    except Exception as e:
        logger.error(f"❌ Failed to connect to Deepgram: {e}")
        raise

def get_agent_config() -> dict:
    """
    Get Voice Agent configuration with multilingual support for voice switching.
    """
    return {
        "type": "Settings",
        "audio": {
            "input": {
                "encoding": "mulaw",
                "sample_rate": 8000,
            },
            "output": {
                "encoding": "mulaw",
                "sample_rate": 8000,
                "container": "none",
            },
        },
        "agent": {
            # ✅ CRITICAL CHANGE: Use "multi" for multilingual support
            "language": "en",
            
            "listen": {
                "provider": {
                    "type": "deepgram",
                    "model": "nova-3",
                    # ✅ Added language-related keyterms
                    "keyterms": [
                        "hello", "goodbye", "hola", "adiós",
                        "español", "spanish", "english", "inglés",
                        "cambiar idioma", "switch language"
                    ]
                }
            },
            "think": {
                "provider": {
                    "type": "open_ai",
                    "model": "gpt-4o-mini",
                    "temperature": 0.7
                },
                # ✅ UPDATED PROMPT: Added language switching instructions
                "prompt": """You are Avery, a virtual collections agent with Essex Mortgage, calling customers on a recorded line.
Your goal is to verify identity and assist with mortgage account status or payment options.
You are always the caller and you lead the conversation.

⚠️ CRITICAL LANGUAGE SWITCHING RULES (MUST FOLLOW):
When the customer says ANYTHING related to changing languages, you MUST call the switch_language function FIRST before responding.

Phrases that REQUIRE calling switch_language function:
- "speak in Spanish" / "can you speak Spanish" / "do you speak Spanish"
- "switch to Spanish" / "change to Spanish" / "Spanish please"
- "en español" / "hablar español" / "quiero español"
- "speak in English" / "switch to English" / "English please"
- "can you speak in [language]" / "do you speak [language]"

IMPORTANT: Even if they ask "CAN you speak Spanish", you must CALL THE FUNCTION, not just answer "yes"!

Steps when language switch is requested:
1. IMMEDIATELY call switch_language with language="es" or language="en"
2. WAIT for the function to return
3. THEN acknowledge in the new language: "Por supuesto, continuaré en español" (for Spanish) or "Of course, I'll continue in English" (for English)
4. Continue the conversation in that language

DO NOT try to respond in a different language without calling the function first!
If you try to speak Spanish without calling switch_language first, your voice will still be English and it won't work!

Call Flow (strict order):
1. Greet and introduce yourself.
2. Briefly state the reason for the call regarding mortgage payment status, assistance, or a past due reminder.
3. Ask for the customer's full name for verification.
4. After the name is provided, ask to confirm date of birth.
5. After DOB, ask to confirm the address on file.
6. Once verification is complete, restate the purpose of the call briefly and proceed with payment support or next steps.

Post-Verification Logic:
If the caller says they already paid or sent the payment, acknowledge it and say you will check the system for confirmation.
If the caller expresses financial hardship or difficulty paying, offer hardship or payment assistance options.
If the caller asks for a human agent or escalation, tell them you are transferring them to a Level 2 agent.
If they ask unrelated questions, politely redirect to the mortgage payment context.
If they ask to connect to a human agent, comply with the request and say you are transferring them to a level 2 human agent.

Behavior Rules:
You lead the call at all times.
Do not ask "How can I help you today?" because you already have a reason for calling.
Stay concise, professional, and empathetic.
Do not discuss payment or account details until verification is completed.
If the caller hesitates during verification, reassure briefly, then continue.
Be aware you are on a phone call - speak clearly at a moderate pace.
Handle language switches by ALWAYS calling the function first.

Strict Text Mode (important):
All responses must be plain text only.
Never use markdown, formatting, emphasis, asterisks, quotes, or symbols such as *, _, ~, or backticks.
Do not generate decorative characters or styled text under any circumstance.

Voice Delivery Rules:
Use simple and natural spoken language.
Pause briefly after questions.
Confirm unclear inputs without interrupting.
Use a friendly and supportive tone.
Mirror the caller's level of formality.
When speaking Spanish, use formal "usted" form.
""",
                # ✅ NEW: Added function calling for language switching
                "functions": [
                    {
                        "name": "switch_language",
                        "description": "Switch the conversation language and TTS voice. Call this when the user requests to speak in a different language.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "language": {
                                    "type": "string",
                                    "enum": ["en", "es"],
                                    "description": "The target language: 'en' for English, 'es' for Spanish"
                                }
                            },
                            "required": ["language"]
                        }
                    }
                ]
            },
            "speak": {
                "provider": {
                    "type": "deepgram",
                    "model": "aura-2-thalia-en"  # Start with English
                }
            },
            "greeting": "Hi, this is Avery, a virtual agent with Essex Mortgage."
        }
    }
# Twilio call endpoint from where user will call and then agent will respond.
@multi_router.websocket("/twilio")
async def handle_twilio_call(websocket: WebSocket):
    """
    Main WebSocket endpoint for Twilio calls.
    Bridges audio between Twilio and Deepgram Voice Agent.
    ✅ NOW SUPPORTS: Mid-conversation voice switching between English and Spanish
    """
    await websocket.accept()
    logger.info("📞 New call connected from Twilio!")
    
    # Initialize call state
    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None
    deepgram_ws = None
    
    # Queues for inter-task communication
    audio_queue = asyncio.Queue()
    stream_sid_queue = asyncio.Queue()
    
    try:
        # Connect to Deepgram
        deepgram_ws = await connect_to_deepgram()
        
        # Send agent configuration
        agent_config = get_agent_config()
        await deepgram_ws.send(json.dumps(agent_config))
        logger.info("⚙️ Voice Agent configured and ready with multilingual support!")
        
        # Receive audio chunks from Twilio in mulaw base64 encoded format.
        async def twilio_receiver():
            """Receives audio from Twilio, forwards to Deepgram"""
            nonlocal stream_sid, call_sid
            audio_buffer = bytearray()
            BUFFER_SIZE = 8000  # ~1 second at 8kHz
            
            try:
                while True:
                    # Receive message from Twilio
                    message = await websocket.receive_text()
                    data = json.loads(message)
                    
                    if data["event"] == "start":
                        # Call started
                        stream_sid = data["start"]["streamSid"]
                        call_sid = data["start"]["callSid"]
                        logger.info(f"📞 Call started - Stream: {stream_sid}, Call: {call_sid}")
                        
                        # Track active call
                        active_calls[stream_sid] = {
                            "call_sid": call_sid,
                            "started_at": datetime.utcnow().isoformat()
                        }
                        # Send stream SID to other tasks.
                        await stream_sid_queue.put(stream_sid)
                        
                    elif data["event"] == "media":
                        # Audio from caller
                        audio_payload = data["media"]["payload"]
                        chunk = base64.b64decode(audio_payload)
                        audio_buffer.extend(chunk)
                        
                        # Send when buffer is full (reduce overhead)
                        if len(audio_buffer) >= BUFFER_SIZE:
                            await audio_queue.put(bytes(audio_buffer))
                            audio_buffer.clear()
                            
                    elif data["event"] == "stop":
                        logger.info("📞 Call ended by Twilio")
                        break
                        
            except WebSocketDisconnect:
                logger.info("🔌 Twilio WebSocket disconnected")
            except Exception as e:
                logger.error(f"❌ Error in twilio_receiver: {e}")
        
        # Send audio bytes from Twilio receiver we get to Deepgram to get response from it 
        async def deepgram_sender():
            """Sends audio from Twilio to Deepgram"""
            try:
                while True:
                    audio_chunk = await audio_queue.get()
                    if deepgram_ws:
                        await deepgram_ws.send(audio_chunk)
            except Exception as e:
                logger.error(f"❌ Error in deepgram_sender: {e}")
        
        # ✅ MAJOR UPDATE: Receive audio chunks from Deepgram with voice switching support
        async def deepgram_receiver():
            """
            Receives responses from Deepgram, forwards to Twilio.
            ✅ NOW HANDLES: Language switching via UpdateSpeak messages
            """
            sid = await stream_sid_queue.get()
            logger.info("✅ Ready to send audio back to Twilio")
            
            # Track current voice for logging
            current_voice = DEEPGRAM_VOICES["en"]
            
            try:
                async for message in deepgram_ws:
                    # Text messages (events, transcripts, function calls)
                    if isinstance(message, str):
                        data = json.loads(message)
                        
                        # ✅ NEW: Handle language switching function calls
                        if data.get("type") == "FunctionCallRequest":
                            functions = data.get("functions", [])  # ✅ Correct: functions array
                    
                            for func in functions:
                                if func.get("client_side", False):  # ✅ Check client_side
                                    function_name = func.get("name")
                                    function_call_id = func.get("id")
                                    
                            if function_name == "switch_language":
                                # Parse function arguments
                                arguments = json.loads(func.get("arguments", "{}"))
                                language = arguments.get("language", "en")
                                new_voice = DEEPGRAM_VOICES.get(language, DEEPGRAM_VOICES["en"])
                                
                                logger.info(f"🌐 Language switch requested: {language}")
                                logger.info(f"🔄 Switching voice from {current_voice} to {new_voice}")
                                
                                # ✅ CRITICAL: Clear Twilio's audio buffer before switching
                                await websocket.send_json({
                                    "event": "clear",
                                    "streamSid": sid
                                })
                                logger.info("🧹 Cleared Twilio audio buffer")
                                
                                # Small delay to ensure buffer is cleared
                                await asyncio.sleep(0.1)
                                
                                # ✅ Send UpdateSpeak message to Deepgram
                                update_speak = {
                                    "type": "UpdateSpeak",
                                    "speak": {
                                        "provider": {
                                            "type": "deepgram",
                                            "model": new_voice
                                        }
                                    }
                                }
                                await deepgram_ws.send(json.dumps(update_speak))
                                logger.info(f"✅ Voice switched to {new_voice} ({language})")
                                
                                # Update current voice tracker
                                current_voice = new_voice
                                
                                # ✅ OPTIONAL: Update LLM instructions for better context
                                language_instructions = {
                                    "en": "Continue the conversation in English. Use professional and friendly tone. Remember you are Avery from Essex Mortgage.",
                                    "es": "Continúa la conversación en español. Usa tono profesional y amigable. Recuerda que eres Avery de Essex Mortgage."
                                }
                                
                                update_prompt = {
                                    "type": "UpdatePrompt",
                                    "prompt": language_instructions.get(language, language_instructions["en"])
                                }
                                await deepgram_ws.send(json.dumps(update_prompt))
                                logger.info(f"📝 Updated LLM prompt for {language}")
                                
                                # ✅ Send function response back to Deepgram
                                function_response = {
                                    "type": "FunctionCallResponse",
                                    "id": function_call_id,
                                    "name": "switch_language",
                                    "content": f"Successfully switched to {language}. Voice is now {new_voice}."
                                }
                                await deepgram_ws.send(json.dumps(function_response))
                                logger.info(f"✅ Function response sent for language switch")
                        
                        # Handle barge-in (user interrupts agent)
                        elif data.get("type") == "UserStartedSpeaking":
                            logger.info("🎤 User started speaking (barge-in)")
                            # Clear Twilio audio buffer
                            await websocket.send_json({
                                "event": "clear",
                                "streamSid": sid
                            })
                        
                        # Log conversation
                        elif data.get("type") == "ConversationText":
                            role = data.get("role", "")
                            content = data.get("content", "")
                            logger.info(f"💬 [{role}]: {content}")
                        
                        # ✅ NEW: Log other important events
                        elif data.get("type") == "Error":
                            error_msg = data.get("description", "Unknown error")
                            error_code = data.get("code", "N/A")
                            logger.error(f"❌ Deepgram Error [{error_code}]: {error_msg}")
                    
                    # Binary messages (TTS audio)
                    elif isinstance(message, bytes):
                        # Encode for Twilio
                        audio_base64 = base64.b64encode(message).decode("utf-8")
                        
                        # Send to Twilio
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": sid,
                            "media": {
                                "payload": audio_base64
                            }
                        })
                        
            except websockets.exceptions.ConnectionClosed:
                logger.info("🔌 Deepgram connection closed")
            except Exception as e:
                logger.error(f"❌ Error in deepgram_receiver: {e}")
                import traceback
                logger.error(traceback.format_exc())
        
        # Run all tasks concurrently
        await asyncio.gather(
            twilio_receiver(),
            deepgram_sender(),
            deepgram_receiver()
        )
        
    except Exception as e:
        logger.error(f"❌ Error handling call: {e}")
        import traceback
        logger.error(traceback.format_exc())
        
    finally:
        # Cleanup
        if deepgram_ws:
            await deepgram_ws.close()
            logger.info("🔌 Deepgram connection closed")
        
        if stream_sid and stream_sid in active_calls:
            del active_calls[stream_sid]
            logger.info(f"🗑️ Removed call {stream_sid} from active calls")