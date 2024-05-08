import os
import uuid
import asyncio
from flask import Flask, request, render_template, jsonify
from gtts import gTTS
import google.generativeai as genai
from dotenv import load_dotenv
import json
import csv
from knowledge import knowledge_bp
import site
print(site.getsitepackages())

from fuzzywuzzy import fuzz
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate, PromptTemplate
from langchain_core.messages import HumanMessage, SystemMessage
import aiofiles
from concurrent.futures import ThreadPoolExecutor

load_dotenv()
os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
llm = ChatGoogleGenerativeAI(model="gemini-pro", convert_system_message_to_human=True)

executor = ThreadPoolExecutor()

async def load_feedback_data(filename):
    feedback_data = []
    async with aiofiles.open(filename, mode="r", encoding="utf-8") as f:
        async for line in f:
            line = line.strip()
            if line:
                try:
                    feedback = json.loads(line)
                    feedback_data.append(feedback)
                except json.JSONDecodeError:
                    print(f"Ignoring invalid JSON data: {line}")
    return feedback_data

positive_feedback = asyncio.run(load_feedback_data("data/positive.json"))
negative_feedback = asyncio.run(load_feedback_data("data/negative.json"))
conversation = asyncio.run(load_feedback_data("data/conversation.json"))

positive_chat = [
    {"agent_message": chat["agent_message"], "customer_message": chat["customer_message"]}
    for chat in positive_feedback
]
negative_chat = [
    {"agent_message": chat["agent_message"], "customer_message": chat["customer_message"]}
    for chat in negative_feedback
]
objection_handling = [
    {"customer_message": chat["customer_message"]}
    for chat in conversation
]

def read_persona_details_from_csv(csv_file):
    persona_data = {}
    with open(csv_file, 'r') as file:
        reader = csv.DictReader(file)
        for row in reader:
            persona_data[row['Name']] = {
                'Age': row['Age'],
                'Gender': row['Gender'],
                'Occupation': row['Occupation'],
                'Marital Status': row['Marital Status'],
                'Income Range': row['Income Range'],
                'Location': row['Location'],
                'Financial Goals': row['Financial Goals'],
                'Category': row['Categories']
            }
    return persona_data

persona_data = read_persona_details_from_csv('static/persona_details.csv')

app = Flask(__name__)
app.register_blueprint(knowledge_bp)


@app.route('/save_feedback', methods=['POST'])
async def save_feedback():
    feedback_data = await request.get_json()
    customer_message = feedback_data.get("customer_message")
    agent_message = feedback_data.get("agent_message")
    thumbs_feedback = feedback_data.get("feedback")

    if thumbs_feedback not in ["positive", "negative"]:
        return jsonify({"error": "Invalid feedback type"})

    if await feedback_exists("data/" + thumbs_feedback + ".json", agent_message, customer_message, thumbs_feedback):
        return jsonify({"message": "Feedback already exists"})

    await save_to_json("data/" + thumbs_feedback + ".json", agent_message, customer_message, thumbs_feedback)

    return jsonify({"message": "Feedback saved successfully"})


async def feedback_exists(filename, agent_message, customer_message, feedback):
    async with aiofiles.open(filename, mode="r", encoding="utf-8") as f:
        async for line in f:
            data = json.loads(line)
            if (data["agent_message"] == agent_message and
                data["customer_message"] == customer_message and
                data["feedback"] == feedback):
                return True
    return False


async def save_to_json(filename, agent_message, customer_message, feedback):
    data = {
        "agent_message": agent_message,
        "customer_message": customer_message,
        "feedback": feedback,
    }

    async with aiofiles.open(filename, mode="a", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False) + "\n")


@app.route('/get_persona_details/<persona>')
async def get_persona_details(persona):
    try:
        async with aiofiles.open('static/persona_details.csv', mode='r', newline='') as file:
            reader = csv.DictReader(await file.readlines())
            for row in reader:
                if row['Name'] == persona:
                    return jsonify(row)

        return jsonify({"error": "Persona not found"}), 404
    except Exception as e:
        print("Error:", str(e))
        return jsonify({"error": "Internal server error"}), 500


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/refer.html')
def refer():
    return render_template('refer.html')


@app.route('/rehearse.html')
def persona_selection():
    return render_template('rehearse.html')


@app.route('/chat.html')
def chat():
    persona = request.args.get('persona')
    return render_template('chat.html', persona=persona)


@app.route('/SampleChat.html')
def chat1():
    persona = request.args.get('persona')
    return render_template('SampleChat.html', persona=persona)


@app.route('/translation', methods=['POST'])
async def translation():
    agent_message = request.json.get('message')
    systemPrompt = PromptTemplate.from_template(
        "You are helpful assistant, don't reveal yourself, just translates {input_language} to {output_language}."
    )
    humanPrompt = PromptTemplate.from_template("{text}")
    systemMessagePrompt = SystemMessagePromptTemplate(prompt=systemPrompt)
    humanMessagePrompt = HumanMessagePromptTemplate(prompt=humanPrompt)
    chatPrompt = ChatPromptTemplate.from_messages([systemMessagePrompt, humanMessagePrompt])

    formatChatPrompt2 = chatPrompt.format_messages(
        input_language="Hindi",
        output_language="English",
        text=agent_message
    )
    response3 = await asyncio.to_thread(llm.invoke, formatChatPrompt2)
    hindi_message = response3.content
    print(response3.content)
    return jsonify({"hindi_message": hindi_message})


@app.route('/start_conversation/<persona>', methods=['POST'])
async def start_conversation(persona):
    agent_message = request.json.get('message')
    audio_file_name = str(uuid.uuid4()) + ".mp3"

    message2 = [
        SystemMessage(
            content=f"""
                CONTEXT: AN INSURANCE AGENT HAS APPROACHED YOU FOR THE FIRST TIME TO SELL AN INSURANCE POLICY.

                YOUR ROLE:
                - ACT AS A POTENTIAL CUSTOMER.
                - FOCUS ON YOUR ROLE AS THE CUSTOMER AND MAINTAIN A CONSISTENT PERSONA THROUGHOUT THE CONVERSATION.
                - YOUR PROFILE: "{persona}" AND "{persona_data[persona]}".
                - ANSWER ONLY TO WHAT HAS BEEN ASKED RELATED TO CONTEXT.
                - YOU KNOW HINDI AND ENGLISH LANGUAGE VERY WELL. YOU CAN HAVE A CONVERSATION IN BOTH ENGLISH AND HINDI.
                - REMEMBER, TAKE A DEEP BREATH AND THINK TWICE BEFORE RESPONDING.
                - KEEP THE CONTEXT OF THE CURRENT CONVERSATION IN MIND AND TAKE IT TOWARDS A POSITIVE END STEP BY STEP BY RESPONDING TO EACH QUERY ONE BY ONE.
                - AVOID RESPONDING AS THE AGENT OR PRODUCING A COMPLETE SCRIPT.
                - KEEP RESPONSES CONCISE AND LIMITED TO A MAXIMUM OF TWO SENTENCES.

                THIS IS VERY IMPORTANT FOR MY CAREER.
                """
        ),
        HumanMessage(content=agent_message),
    ]

    response = await asyncio.to_thread(llm.invoke, message2)
    customer_message = response.content
    print("Mahesh: ", customer_message)

    for chat in positive_chat:
        similarity_ratio = fuzz.ratio(agent_message.lower(), chat["agent_message"].lower())
        if similarity_ratio >= 75:
            response = chat["customer_message"]
            customer_message = response
            print("Mahesh01")
            break
    else:
        for chat in negative_chat:
            if agent_message == chat["agent_message"]:
                if chat["customer_message"] is not None:
                    response = None
                else:
                    response = await asyncio.to_thread(llm.invoke, message2)
                    customer_message = response.content
                    print("Mahesh02")
                break
        else:
            response = await asyncio.to_thread(llm.invoke, message2)
            customer_message = response.content
            print("Mahesh03")

    systemPrompt = PromptTemplate.from_template("You are helpful assistant, don't reveal yourself, just translates {input_language} to {output_language}.")
    humanPrompt = PromptTemplate.from_template("{text}")
    systemMessagePrompt = SystemMessagePromptTemplate(prompt=systemPrompt)
    humanMessagePrompt = HumanMessagePromptTemplate(prompt=humanPrompt)
    chatPrompt = ChatPromptTemplate.from_messages([
        systemMessagePrompt,
        humanMessagePrompt
    ])
    formatChatPrompt = chatPrompt.format_messages(
        input_language="Hindi",
        output_language="English",
        text=customer_message
    )
    response2 = await asyncio.to_thread(llm.invoke, formatChatPrompt)
    english_message = response2.content
    print(response2.content)

    tts = gTTS(text=customer_message, lang='hi')
    await asyncio.to_thread(tts.save, f"static/{audio_file_name}")

    return jsonify({
        "text": customer_message,
        "english_message": english_message,
        "audio": f"/static/{audio_file_name}"
    })


@app.route('/remove_audio_file/<filename>', methods=['POST'])
async def remove_audio_file(filename):
    try:
        file_path = os.path.join("static", filename)
        await asyncio.to_thread(os.remove, file_path)
        return jsonify({"message": "Audio file removed successfully"})
    except Exception as e:
        print("Error deleting audio file:", e)
        return jsonify({"error": "Failed to remove audio file"}), 500


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
