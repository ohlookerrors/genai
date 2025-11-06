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

        call = twillio_client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/twilml",
            status_callback=f"{SERVER_URL}/call_status",
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

@multi_router.api_route("/twilml", methods=["GET", "POST"])
async def generate_twiml():
    response = VoiceResponse()
    connect = Connect()
    websocket_url = f"{SERVER_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/twilio"
    stream = Stream(url=websocket_url)
    connect.append(stream)
    response.append(connect)
    logger.info(f"Generated TwiML for outbound call")
    return Response(content=response.to_xml(), media_type="application/xml")

@multi_router.post("/call_status")
async def call_status_webhook(
    CallSid: str,
    CallStatus: str,
    From: Optional[str] = None,
    To: Optional[str] = None
):
    logger.info(f"Call status update - SID: {CallSid}, Status: {CallStatus}")
    return {"status": "received"}

# Deepgram API key
api_key = os.getenv("DEEPGRAM_API_KEY")

# Store active calls
active_calls: Dict[str, dict] = {}

# Voice mappings for Deepgram TTS
DEEPGRAM_VOICES = {
    "en": "aura-2-thalia-en",
    "es": "aura-2-celeste-es"
}

# Greetings for each language
GREETINGS = {
    "en": "Hi, this is Avery, a virtual agent with Essex Mortgage.",
    "es": "Hola, soy Avery, un agente virtual de Essex Mortgage."
}

async def connect_to_deepgram():
    """Establish WebSocket connection to Deepgram Voice Agent API."""
    deepgram_url = "wss://agent.deepgram.com/v1/agent/converse"
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

def get_agent_config(language: str = "en", conversation_history: list = None) -> dict:
    """
    Get Voice Agent configuration for specific language.
    
    Args:
        language: 'en' or 'es'
        conversation_history: Optional conversation history to restore context
    """
    
    # Language-specific prompts
    prompts = {
        "en": """You are Avery, a virtual collections agent with Essex Mortgage, calling customers on a recorded line.
Your goal is to verify identity and assist with mortgage account status or payment options.
You are always the caller and you lead the conversation.

⚠️ CRITICAL LANGUAGE SWITCHING RULES (MUST FOLLOW):
When the customer says ANYTHING related to changing to Spanish, you MUST call the switch_language function FIRST before responding.

Phrases that REQUIRE calling switch_language function:
- "speak in Spanish" / "can you speak Spanish" / "do you speak Spanish"
- "switch to Spanish" / "change to Spanish" / "Spanish please"
- "en español" / "hablar español" / "quiero español"
- If the customer directly speaks Spanish (e.g., "Hola", "¿Cómo estás?")

IMPORTANT: Even if they ask "CAN you speak Spanish", you must CALL THE FUNCTION to switch!

Steps when language switch is requested:
1. IMMEDIATELY call switch_language with language="es"
2. Wait for confirmation
3. The system will reconnect in Spanish

Call Flow (strict order):
1. Greet and introduce yourself.
2. Briefly state the reason for the call regarding mortgage payment status.
3. Ask for the customer's full name for verification.
4. After the name is provided, ask to confirm date of birth.
5. After DOB, ask to confirm the address on file.
6. Once verification is complete, proceed with payment support or next steps.

Post-Verification Logic:
- If they already paid, acknowledge and say you'll check the system.
- If financial hardship, offer assistance options.
- If they ask for human agent, transfer to Level 2 agent.

Behavior Rules:
- Lead the call at all times.
- Stay concise, professional, and empathetic.
- Do not discuss payment details until verification is completed.
- Be aware you are on a phone call - speak clearly at moderate pace.

Strict Text Mode:
All responses must be plain text only. Never use markdown or formatting symbols.

Voice Delivery Rules:
- Use simple and natural spoken language.
- Pause briefly after questions.
- Use a friendly and supportive tone.
""",
        "es": """Eres Avery, un agente virtual de cobros de Essex Mortgage, llamando a clientes en una línea grabada.
Tu objetivo es verificar la identidad y ayudar con el estado de la cuenta hipotecaria u opciones de pago.
Tú siempre eres quien llama y lideras la conversación.

⚠️ REGLAS CRÍTICAS DE CAMBIO DE IDIOMA (DEBE SEGUIR):
Cuando el cliente diga CUALQUIER COSA relacionada con cambiar a inglés, DEBES llamar a la función switch_language PRIMERO antes de responder.

Frases que REQUIEREN llamar a la función switch_language:
- "speak in English" / "habla inglés" / "inglés por favor"
- "switch to English" / "change to English" / "English please"
- "en inglés" / "quiero inglés"
- Si el cliente habla directamente en inglés

IMPORTANTE: Incluso si preguntan "¿PUEDES hablar inglés?", debes LLAMAR LA FUNCIÓN para cambiar!

Pasos cuando se solicita cambio de idioma:
1. INMEDIATAMENTE llama switch_language con language="en"
2. Espera confirmación
3. El sistema se reconectará en inglés

Flujo de Llamada (orden estricto):
1. Saluda y preséntate.
2. Indica brevemente el motivo de la llamada sobre el estado del pago hipotecario.
3. Pide el nombre completo del cliente para verificación.
4. Después del nombre, pide confirmar la fecha de nacimiento.
5. Después de la fecha, pide confirmar la dirección registrada.
6. Una vez completada la verificación, procede con el soporte de pago.

Lógica Post-Verificación:
- Si ya pagaron, reconócelo y di que verificarás el sistema.
- Si hay dificultades financieras, ofrece opciones de asistencia.
- Si piden un agente humano, transfiere a un agente de Nivel 2.

Reglas de Comportamiento:
- Lidera la llamada en todo momento.
- Sé conciso, profesional y empático.
- No discutas detalles de pago hasta completar la verificación.
- Ten en cuenta que estás en una llamada telefónica - habla claramente a ritmo moderado.

Modo de Texto Estricto:
Todas las respuestas deben ser texto plano solamente. Nunca uses markdown o símbolos de formato.

Reglas de Entrega de Voz:
- Usa lenguaje hablado simple y natural.
- Haz pausas breves después de preguntas.
- Usa un tono amigable y de apoyo.
- Usa la forma formal "usted".
"""
    }
    
    config = {
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
            "language": language,
            
            "listen": {
                "provider": {
                    "type": "deepgram",
                    "model": "nova-3",
                    "keyterms": [
                        # English keywords
                        "hello", "goodbye", "Essex Mortgage",
                        "spanish", "español", "habla español",
                        # Spanish keywords
                        "hola", "adiós", "inglés", "english",
                        "switch language", "cambiar idioma"
                    ]
                }
            },
            "think": {
                "provider": {
                    "type": "open_ai",
                    "model": "gpt-4o-mini",
                    "temperature": 0.7
                },
                "prompt": prompts.get(language, prompts["en"]),
                "functions": [
                    {
                        "name": "switch_language",
                        "description": "Switch the conversation language. Call this when the user requests to speak in a different language.",
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
                    "model": DEEPGRAM_VOICES[language]
                }
            },
            "greeting": GREETINGS[language]
        }
    }
    
    # Add conversation history if provided
    if conversation_history:
        config["agent"]["context"] = {
            "messages": conversation_history
        }
    
    return config

# Global variable to signal language switch
language_switch_signal = {}

@multi_router.websocket("/twilio")
async def handle_twilio_call(websocket: WebSocket):
    """
    Main WebSocket endpoint for Twilio calls.
    ✅ NOW SUPPORTS: Language switching via reconnection
    """
    await websocket.accept()
    logger.info("📞 New call connected from Twilio!")
    
    # Initialize call state
    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None
    deepgram_ws = None
    current_language = "en"
    conversation_history = []
    
    # Queues for inter-task communication
    audio_queue = asyncio.Queue()
    stream_sid_queue = asyncio.Queue()
    reconnect_event = asyncio.Event()
    
    async def initialize_deepgram_connection(language: str, history: list = None):
        """Initialize or reinitialize Deepgram connection with specified language."""
        nonlocal deepgram_ws
        
        # Close existing connection if any
        if deepgram_ws:
            try:
                await deepgram_ws.close()
                logger.info("🔌 Closed previous Deepgram connection")
            except:
                pass
        
        # Connect to Deepgram
        deepgram_ws = await connect_to_deepgram()
        
        # Send agent configuration
        agent_config = get_agent_config(language, history)
        await deepgram_ws.send(json.dumps(agent_config))
        logger.info(f"⚙️ Voice Agent configured for language: {language}")
        
        return deepgram_ws
    
    try:
        # Initial connection
        deepgram_ws = await initialize_deepgram_connection(current_language)
        
        # Twilio receiver
        async def twilio_receiver():
            """Receives audio from Twilio, forwards to Deepgram"""
            nonlocal stream_sid, call_sid
            audio_buffer = bytearray()
            BUFFER_SIZE = 8000
            
            try:
                while True:
                    message = await websocket.receive_text()
                    data = json.loads(message)
                    
                    if data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        call_sid = data["start"]["callSid"]
                        logger.info(f"📞 Call started - Stream: {stream_sid}, Call: {call_sid}")
                        
                        active_calls[stream_sid] = {
                            "call_sid": call_sid,
                            "started_at": datetime.utcnow().isoformat(),
                            "language": current_language
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
                logger.info("🔌 Twilio WebSocket disconnected")
            except Exception as e:
                logger.error(f"❌ Error in twilio_receiver: {e}")
        
        # Deepgram sender
        async def deepgram_sender():
            """Sends audio from Twilio to Deepgram"""
            try:
                while True:
                    # Wait for either audio or reconnect signal
                    try:
                        audio_chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                        if deepgram_ws and not reconnect_event.is_set():
                            await deepgram_ws.send(audio_chunk)
                    except asyncio.TimeoutError:
                        # Check if reconnect is needed
                        if reconnect_event.is_set():
                            await asyncio.sleep(0.1)
                        continue
            except Exception as e:
                logger.error(f"❌ Error in deepgram_sender: {e}")
        
        # Deepgram receiver
        async def deepgram_receiver():
            """
            Receives responses from Deepgram, forwards to Twilio.
            ✅ Handles language switching via reconnection
            """
            nonlocal current_language, conversation_history, deepgram_ws
            
            sid = await stream_sid_queue.get()
            logger.info("✅ Ready to send audio back to Twilio")
            
            try:
                while True:
                    # Check if we need to reconnect
                    if reconnect_event.is_set():
                        logger.info("🔄 Reconnection in progress, pausing receiver...")
                        await asyncio.sleep(0.5)
                        continue
                    
                    try:
                        # ✅ FIX: Use recv() instead of __anext__()
                        message = await asyncio.wait_for(deepgram_ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        logger.info("🔌 Deepgram connection closed during receive")
                        if not reconnect_event.is_set():
                            # If not intentional reconnection, break
                            break
                        continue
                    
                    # Text messages
                    if isinstance(message, str):
                        data = json.loads(message)
                        
                        # Handle language switching function calls
                        if data.get("type") == "FunctionCallRequest":
                            functions = data.get("functions", [])
                    
                            for func in functions:
                                if func.get("client_side", False):
                                    function_name = func.get("name")
                                    function_call_id = func.get("id")
                                    
                                    if function_name == "switch_language":
                                        arguments = json.loads(func.get("arguments", "{}"))
                                        target_language = arguments.get("language", "en")
                                        
                                        if target_language == current_language:
                                            logger.info(f"ℹ️ Already in {target_language}, ignoring switch")
                                            # Send function response to avoid hanging
                                            function_response = {
                                                "type": "FunctionCallResponse",
                                                "id": function_call_id,
                                                "name": "switch_language",
                                                "content": f"Already speaking in {target_language}"
                                            }
                                            await deepgram_ws.send(json.dumps(function_response))
                                            continue
                                        
                                        logger.info(f"🌐 Language switch requested: {current_language} → {target_language}")
                                        
                                        # Clear Twilio's audio buffer
                                        await websocket.send_json({
                                            "event": "clear",
                                            "streamSid": sid
                                        })
                                        logger.info("🧹 Cleared Twilio audio buffer")
                                        
                                        # Set reconnect signal to pause other tasks
                                        reconnect_event.set()
                                        
                                        # Small delay to ensure audio stops
                                        await asyncio.sleep(0.3)
                                        
                                        # Save conversation history (last 10 exchanges)
                                        history_to_save = conversation_history[-10:] if len(conversation_history) > 10 else conversation_history
                                        
                                        # Reconnect with new language
                                        logger.info(f"🔄 Reconnecting Voice Agent in {target_language}...")
                                        try:
                                            deepgram_ws = await initialize_deepgram_connection(target_language, history_to_save)
                                            current_language = target_language
                                            
                                            # Update active call record
                                            if sid in active_calls:
                                                active_calls[sid]["language"] = target_language
                                            
                                            logger.info(f"✅ Successfully reconnected in {target_language}")
                                        except Exception as reconnect_error:
                                            logger.error(f"❌ Failed to reconnect: {reconnect_error}")
                                        finally:
                                            # Clear reconnect signal
                                            reconnect_event.clear()
                                        
                                        continue
                        
                        # Handle barge-in
                        elif data.get("type") == "UserStartedSpeaking":
                            logger.info("🎤 User started speaking (barge-in)")
                            await websocket.send_json({
                                "event": "clear",
                                "streamSid": sid
                            })
                        
                        # Log and save conversation
                        elif data.get("type") == "ConversationText":
                            role = data.get("role", "")
                            content = data.get("content", "")
                            logger.info(f"💬 [{role}] [{current_language}]: {content}")
                            
                            # Save to history
                            conversation_history.append({
                                "type": "History",
                                "role": role,
                                "content": content
                            })
                        
                        # Log errors
                        elif data.get("type") == "Error":
                            error_msg = data.get("description", "Unknown error")
                            error_code = data.get("code", "N/A")
                            logger.error(f"❌ Deepgram Error [{error_code}]: {error_msg}")
                    
                    # Binary messages (TTS audio)
                    elif isinstance(message, bytes):
                        if not reconnect_event.is_set():
                            audio_base64 = base64.b64encode(message).decode("utf-8")
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