from flask import Flask, request, render_template, jsonify
from gtts import gTTS
import google.generativeai as genai
import uuid # for random filename
import os
from dotenv import load_dotenv
import json
import csv
from knowledge import knowledge_bp
from fuzzywuzzy import fuzz #for similarity ratio of text
#from langchain.llms import OpenAI
#from SecretKey import OPENAI_API_KEY



load_dotenv()
os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
#os.environ['OPENAI_API_KEY'] = OPENAI_API_KEY


from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate, HumanMessagePromptTemplate, PromptTemplate
#from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
#
llm = ChatGoogleGenerativeAI(model="gemini-pro", convert_system_message_to_human=True)
#llm = ChatOpenAI(model="gpt-3.5-turbo", convert_system_message_to_human=True)
#llm = ChatOpenAI(model="davinci-002")
#llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro-latest", convert_system_message_to_human=True)

def load_feedback_data(filename):
    feedback_data = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()  # Remove leading/trailing whitespace
            if line:  # Check if line is not empty
                try:
                    feedback = json.loads(line)
                    feedback_data.append(feedback)
                except json.JSONDecodeError:
                    print(f"Ignoring invalid JSON data: {line}")
    return feedback_data


positive_feedback = load_feedback_data("data/positive.json")
negative_feedback = load_feedback_data("data/negative.json")
conversation = load_feedback_data("data/conversation.json")

#print("conversation: ", conversation)
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

#print ("objection_handling: ",objection_handling)
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

# Example usage
persona_data = read_persona_details_from_csv('static/persona_details.csv')
#print("ppp",persona_data)

app = Flask(__name__)

# Register blueprints for different parts of the application
app.register_blueprint(knowledge_bp)

@app.route('/save_feedback', methods=['POST'])
def save_feedback():
    feedback_data = request.json
    customer_message = feedback_data.get("customer_message")
    agent_message = feedback_data.get("agent_message")
    thumbs_feedback = feedback_data.get("feedback")

    # Determine the filename based on thumbs feedback
    if thumbs_feedback == "positive" or thumbs_feedback == "negative":
        filename = thumbs_feedback + ".json"
    else:
        return jsonify({"error": "Invalid feedback type"})

    # Check if feedback already exists
    if feedback_exists(filename, agent_message, customer_message, thumbs_feedback):
        return jsonify({"message": "Feedback already exists"})

    # Save the feedback data to the appropriate JSON file
    save_to_json(filename, agent_message, customer_message, thumbs_feedback)

    return jsonify({"message": "Feedback saved successfully"})

def feedback_exists(filename, agent_message, customer_message, feedback):
    with open(os.path.join("data", filename), "r", encoding="utf-8") as f:
        for line in f:
            data = json.loads(line)
            if (data["agent_message"] == agent_message and
                data["customer_message"] == customer_message and
                data["feedback"] == feedback):
               # print("Data exists")
                return True
    return False

def save_to_json(filename, agent_message, customer_message, feedback):
    data = {
        "agent_message": agent_message,
        "customer_message": customer_message,
        "feedback": feedback,
    }

    with open(os.path.join("data", filename), "a", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.write("\n")

@app.route('/get_persona_details/<persona>')
def get_persona_details(persona):
    try:
        # Read data from CSV file
        with open('static/persona_details.csv', 'r', newline='') as file:
            reader = csv.DictReader(file)
            # Find the persona details based on the requested persona name
            for row in reader:
                if row['Name'] == persona:
                    #return jsonify(row)
                    return row

        # If persona is not found, return a 404 error
        return jsonify({"error": "Persona not found"}), 404
    except Exception as e:
        # Print the specific error that occurred
        print("Error:", str(e))
        # Return a JSON response with an error message and a 500 status code
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

#--------------------------------------------------------------------------------
@app.route('/translation', methods=['POST'])
def translation():
    agent_message = request.json.get('message')
    systemPrompt = PromptTemplate.from_template(
        "You are helpful assistant, don't reveal yourself, just translates {input_language} to {output_language}."
        )
    humanPrompt = PromptTemplate.from_template("{text}")
    systemMessagePrompt = SystemMessagePromptTemplate(prompt=systemPrompt)
    humanMessagePrompt = HumanMessagePromptTemplate(prompt=humanPrompt)
    chatPrompt = ChatPromptTemplate.from_messages([
        systemMessagePrompt,
        humanMessagePrompt
    ])

    formatChatPrompt2 = chatPrompt.format_messages(
        input_language="Hindi",
        output_language="English",
        text=agent_message
    )
    response3 = llm.invoke(formatChatPrompt2)
    hindi_message = response3.content
    print(response3.content)
    # Return the response text and audio URL in JSON format
    return jsonify({
        "hindi_message": hindi_message,
    })


#--------------------------------------------------------------------------------

@app.route('/start_conversation/<persona>', methods=['POST'])
def start_conversation(persona):
    customer_message = ""
    # Generate a random file name for the MP3
    audio_file_name = str(uuid.uuid4()) + ".mp3"
    agent_message = request.json.get('message')
    #print("message: ", message1)
    if agent_message:
        Detail = (persona_data[persona])
        CustName = (persona)
        print("CustName: ", CustName)
        print("Customer Detail: ", Detail)
        message2 = [
            SystemMessage(
                content=f"""
                CONTEXT: AN INSURANCE AGENT HAS APPROACHED YOU FOR THE FIRST TIME TO SELL AN INSURANCE POLICY.

                YOUR ROLE:
                - ACT AS A POTENTIAL CUSTOMER.
                - FOCUS ON YOUR ROLE AS THE CUSTOMER AND MAINTAIN A CONSISTENT PERSONA THROUGHOUT THE CONVERSATION.
                - YOUR PROFILE: "{CustName}" AND "{Detail}".
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

        response = llm.invoke(message2)
        customer_message = response.content
        print("Mahesh: ", customer_message)
        #---------------------------------------------------------------------------------
        #print("positive_chat: ",positive_chat)
        # Check if the user's message is in positive_chat

        for chat in positive_chat:
            similarity_ratio = fuzz.ratio(agent_message.lower(), chat["agent_message"].lower())
            if similarity_ratio >= 75:
                # If the user's message matches, use the corresponding agent message as response
                response = chat["customer_message"]
                customer_message = response
                print("Mahesh01")
                break
        else:
            # Check if the user's message is in negative_chat
            for chat in negative_chat:
                if agent_message == chat["agent_message"]:
                    #print(agent_message)
                    # If the user's message matches, check if the agent response is not None
                    if chat["customer_message"] is not None:
                        response = None  # Do not provide a response
                    else:
                        response = llm.invoke(message2)
                        customer_message = response.content
                        print("Mahesh02")
                    break
            else:
                # If the user's message is not in positive_chat or negative_chat, use llm system response
                response = llm.invoke(message2)
                customer_message = response.content
                print("Mahesh03")
        #-------------------------------------------------------------------------------------------------------------------

        # Chat message Prompt template using Prompttemplate for translation

        systemPrompt = PromptTemplate.from_template("You are helpful assistant, don't reveal yourself, just translates {input_language} to {output_language}."
            )
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
        response2 = llm.invoke(formatChatPrompt)
        english_message = response2.content
        print(response2.content)

        #print("customer_message: ", customer_message)
        # Save the audio file with the generated file name
        tts = gTTS(text=customer_message, lang='hi')
        tts.save(f"static/{audio_file_name}")

        # Delete the audio fi   le after sending the response
       # remove_audio_file(audio_file_name)

        # Return the response text and audio URL in JSON format
        return jsonify({
            "text": customer_message,
            "english_message": english_message,
            "audio": f"/static/{audio_file_name}"  # Provide the URL of the saved audio file
        })

@app.route('/remove_audio_file/<filename>', methods=['POST'])
def remove_audio_file(filename):
    try:
        file_path = os.path.join("static", filename)
        os.remove(file_path)
        #print("Audio file removed:", file_path)
        return jsonify({"message": "Audio file removed successfully"})
    except Exception as e:
        print("Error deleting audio file:", e)
        return jsonify({"error": "Failed to remove audio file"}), 500


#--------------------------------------------------------------------------------


if __name__ == '__main__':
    #app.run(debug=False)
    app.run(host="0.0.0.0", port=8000)
