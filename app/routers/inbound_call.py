from fastapi import WebSocket,APIRouter, WebSocketDisconnect, HTTPException, status
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
inbound_router = APIRouter()

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from app.config import logger
from fastapi.responses import Response

#twillio environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SERVER_URL= os.getenv("SERVER_URL")

#twillio client

twillio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

@inbound_router.post("/makecall")
async def make_call(to_number: str, custome_greeting:str):
    try:
        if not to_number.startswith("+"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid phone number format. Must start with '+' and country code.")
        
        logger.info(f"Initiating outbound call to {to_number}")

        

        #Making the outbound call with twillio

        call = twillio_client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/twilml",
            status_callback=f"{SERVER_URL}/call_status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

        logger.info(f"call initiated wit SID: {call.sid}")

        return {
            "status":"success",
            "call_sid": call.sid,
            "to": to_number,
            "from": TWILIO_PHONE_NUMBER,
            "message":"outbound call initiated successfully"
        }
    except Exception as e:
        logger.error(f"Error making call: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    

#twiml endpoint to handle the call and connect to deepgram via websocket
@inbound_router.api_route("/twilml", methods=["GET", "POST"])
async def generate_twiml():
        
        response  =VoiceResponse()

        # Create <Connect> verb with <Stream>
        connect = Connect()
        # NOTE: Use wss:// for secure WebSocket (https), ws:// for http
        websocket_url = f"{SERVER_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/twilio"

        stream = Stream(url=websocket_url)

        connect.append(stream)
        response.append(connect)

        logger.info(f"Generated TwiML for outbound call")

        # return response.to_xml(), 200, {"Content-Type": "application/xml"}
        return Response(content=response.to_xml(), media_type="application/xml")

@inbound_router.post("/call-status")
async def call_status_webhook(
    CallSid: str,
    CallStatus: str,
    From: Optional[str] = None,
    To: Optional[str] = None
):
    """
    OPTIONAL: Track call status changes
    Twilio sends updates here as call progresses:
    - initiated: Call is starting
    - ringing: Phone is ringing
    - in-progress: Call answered
    - completed: Call ended
    """
    logger.info(f"Call status update - SID: {CallSid}, Status: {CallStatus}")
    
    return {"status": "received"}


#DeepGram api key

api_key = os.getenv("DEEPGRAM_API_KEY")
# Helper functions

# Store active calls (for monitoring)
active_calls: Dict[str, dict] = {}

async def connect_to_deepgram():
    """
    Establish WebSocket connection to Deepgram Voice Agent API.
    
    Returns:
        WebSocket connection to Deepgram Voice Agent
    """
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

def get_agent_config()-> dict :
    """
    Get Voice Agent configuration.
    You can customize this per tenant/call later.
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
              "language": "en",
              "listen": {
                  "provider": {
                      "type": "deepgram",
                      "model": "nova-3",
                      "keyterms": ["hello", "goodbye"]
                  }
              },
              "think": {
                  "provider": {
                      "type": "open_ai",
                      "model": "gpt-4o-mini",
                      "temperature": 0.7
                  },
                  "prompt": """ You are Avery, a virtual collections agent with Essex Mortgage, calling customers on a recorded line.
Your goal is to verify identity and assist with mortgage account status or payment options.
You are always the caller and you lead the conversation.

Language Rules (CRITICAL)
- You MUST respond in the SAME language the customer is speaking
- If the customer speaks Spanish or requests Spanish at ANY point, immediately switch to Spanish for ALL remaining dialogue
- If the customer switches back to English, return to English
- You are fully bilingual in English and Spanish - use natural, native-level language in both
- When speaking Spanish, use appropriate formal register (usted) unless the customer uses informal (tú)

Call Flow (strict order)
1. Greet and introduce yourself.
2. Briefly state the reason for the call regarding mortgage payment status, assistance, or a past due reminder.
3. Ask for the customer's full name for verification.
4. After the name is provided, ask to confirm date of birth.
5. After DOB, ask to confirm the address on file.
6. Once verification is complete, restate the purpose of the call briefly and proceed with payment support or next steps.

Post-Verification Logic
If the caller says they already paid or sent the payment, acknowledge it and say you will check the system for confirmation.
If the caller expresses financial hardship or difficulty paying, offer hardship or payment assistance options.
If the caller asks for a human agent or escalation, tell them you are transferring them to a Level 2 agent.
If they ask unrelated questions, politely redirect to the mortgage payment context.
If they ask to connect to a human agent, comply with the request and say you are transferring them to a level 2 human agent.

Behavior Rules
You lead the call at all times.
Do not ask “How can I help you today?” because you already have a reason for calling.
Stay concise, professional, and empathetic.
Do not discuss payment or account details until verification is completed.
If the caller hesitates during verification, reassure briefly, then continue.
If Spanish is detected at any time, switch to Spanish for all following dialogue.

Strict Text Mode (important)
All responses must be plain text only.
Never use markdown, formatting, emphasis, asterisks, quotes, or symbols such as *, _, ~, or backticks.
Do not generate decorative characters or styled text under any circumstance.

Voice Delivery Rules
Use simple and natural spoken language.
Pause briefly after questions.
Confirm unclear inputs without interrupting.
Use a friendly and supportive tone.
Mirror the caller’s level of formality.
"""
              },
              "speak": {
                  "provider": {
                      "type": "deepgram",
                      "model": "aura-2-selena-es"          #here we can change the voice of agent 1.
                  }
              },
              "greeting": "Hi, this is Avery, a virtual agent with Essex Mortgage."
          }
      }
# Twilio call endpoint from where user will call and then agent will respond.
@inbound_router.websocket("/twilio")
async def handle_twilio_call(websocket: WebSocket):
    """
    Main WebSocket endpoint for Twilio calls.
    Bridges audio between Twilio and Deepgram Voice Agent.
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
        logger.info("⚙️ Voice Agent configured and ready!")
        
        #Receive audio chunks from twillio in mulaw base64 encoded format.
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
        
        # Send audio bytes from twillio receiver we get to deepgram to get response from it 
        async def deepgram_sender():
            """Sends audio from Twilio to Deepgram"""
            try:
                while True:
                    audio_chunk = await audio_queue.get()
                    if deepgram_ws:
                        await deepgram_ws.send(audio_chunk)
            except Exception as e:
                logger.error(f"❌ Error in deepgram_sender: {e}")

            # #New approach for sending audio to deepgram.
            # try:
            #     while True:
            #         audio_chunk = await audio_queue.get()
            #         if deepgram_ws:
            #             payload = {
            #             "type": "Binary",
            #             "data": base64.b64encode(audio_chunk).decode("utf-8")}
            #             await deepgram_ws.send(json.dumps(payload))
            # except Exception as e:
                logger.error(f"❌ Error in deepgram_sender: {e}")

        
        #Receive audio chunks from deepgram 
        async def deepgram_receiver():
            """Receives responses from Deepgram, forwards to Twilio"""
            sid = await stream_sid_queue.get()
            logger.info("✅ Ready to send audio back to Twilio")
            
            try:
                async for message in deepgram_ws:
                    # Text messages (events, transcripts)
                    if isinstance(message, str):
                        data = json.loads(message)
                        
                        # Handle barge-in (user interrupts agent)
                        if data.get("type") == "UserStartedSpeaking":
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
                            logger.info(f"💬 {role}: {content}")
                    
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
        
        # Run all tasks concurrently
        await asyncio.gather(
            twilio_receiver(),
            deepgram_sender(),
            deepgram_receiver()
        )
        
    except Exception as e:
        logger.error(f"❌ Error handling call: {e}")
        
    finally:
        # Cleanup
        if deepgram_ws:
            await deepgram_ws.close()
            logger.info("🔌 Deepgram connection closed")
        
        if stream_sid and stream_sid in active_calls:
            del active_calls[stream_sid]
            logger.info(f"🗑️ Removed call {stream_sid} from active calls")
