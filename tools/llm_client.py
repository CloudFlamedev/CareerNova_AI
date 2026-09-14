from langchain_groq import ChatGroq
import os
from dotenv import load_dotenv

load_dotenv()

llm = ChatGroq(
    model="openai/gpt-oss-20b",     
    api_key=os.getenv("GROQ_API_KEY"),
    temperature=0.7
)
