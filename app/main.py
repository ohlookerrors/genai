from fastapi import FastAPI

# from app.routers.inbound_call import inbound_router
# from app.routers.rag_system_router import voice_router
from app.routers.overall_router import overall_router
from app.routers.multi_language_elevanlabs import multi_router
from dotenv import load_dotenv
load_dotenv()

#fastapi app

app = FastAPI()


#Updated voice router with RAG system
# app.include_router(voice_router)

#Inbound call one was my previous workable code which I written for demo purpose of deepgram voice system.
# app.include_router(inbound_router)



#Below is the dynamic routing for voice changing system AND It's working perfectly fine.
app.include_router(multi_router)



#Below is overall English agent which is working as expected
"""This overall router propmpt is properly working to demo the dummy system to Jamie 
regarding the English language.
"""
# app.include_router(overall_router)