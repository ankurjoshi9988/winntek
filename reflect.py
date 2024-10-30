from flask import request, jsonify, session
from flask_login import login_required, current_user
import random, uuid
import azure.cognitiveservices.speech as speechsdk
import asyncio
from models import Product, ReferConversation, Conversation  # Adjust based on your project structure
from flask import Blueprint
import os
import google.generativeai as genai
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_google_genai import ChatGoogleGenerativeAI
from flask import current_app
from conversation_service import start_conversation, add_message, close_conversation, get_past_conversations, start_refer_conversation, add_refer_message, generate_refer_feedback
from extensions import login_manager, csrf, mail, oauth, db

# Imports
import os
import random
import asyncio
from flask import Blueprint, request, jsonify, current_app, session
#from some_module import genai, ChatGoogleGenerativeAI  # Ensure you import required classes/functions here

# Constants for error messages
INVALID_REQUEST_MSG = "Invalid request: Missing conversation_id, sender, or content"
NO_QUESTIONS_FOUND_MSG = "No questions found for the selected product."
FAILED_LOAD_PRODUCTS_MSG = "Failed to load products"
AUDIO_GENERATION_ERROR_MSG = "Failed to generate audio for the conversation."
FINAL_FEEDBACK_ERROR_MSG = "Failed to generate audio for feedback."

# Blueprints
reflect_bp = Blueprint('reflect', __name__)

# API Keys from environment file
api_key = os.getenv('GOOGLE_API_KEY')
servamapi_key = os.getenv('SERVAM_API_KEY')
azure_subscription_key = os.getenv("AZURE_SUBSCRIPTION_KEY")
azure_region = os.getenv("AZURE_REGION")

# LLM Configuration
genai.configure(api_key=api_key)
llm = ChatGoogleGenerativeAI(model="gemini-pro", convert_system_message_to_human=True, temperature=0.8)

# Voice mappings for personas
VOICE_MAPPING = {
    "Male": "hi-IN-MadhurNeural",
    "Female": "hi-IN-SwaraNeural"
}

# Route to add refer message to conversation
@reflect_bp.route('/add_refer_message', methods=['POST'])
@login_required
def add_refer_message():
    data = request.json
    current_app.logger.info(f"Request Data: {data}")

    conversation_id = data.get('conversation_id')
    sender = data.get('sender')
    content = data.get('content')

    if not conversation_id or not sender or not content:
        current_app.logger.error(INVALID_REQUEST_MSG)
        return jsonify({'error': 'Invalid request'}), 400

    add_message(conversation_id, sender, content)
    return jsonify({'status': 'Message added'}), 200


# Route to close refer conversation and generate feedback
@reflect_bp.route('/close_refer_conversation', methods=['POST'])
@login_required
async def close_refer_conversation_route():
    data = request.json
    conversation_id = data.get('conversation_id')

    conversation = ReferConversation.query.get(conversation_id)
    if not conversation:
        return jsonify({'error': 'Conversation not found'}), 404

    feedback_content = await generate_refer_feedback(conversation)
    return jsonify({'status': 'Conversation closed', 'feedback': feedback_content})


# Route to load products for selection
@reflect_bp.route('/load-products', methods=['GET'])
@login_required
def load_products():
    try:
        products = db.session.query(Product.name).distinct().all()
        product_list = [{"name": product[0]} for product in products]
        return jsonify({"products": product_list})
    except Exception as e:
        current_app.logger.error(f"Error loading products: {e}")
        return jsonify({"error": FAILED_LOAD_PRODUCTS_MSG}), 500

# Function to get questions and answers for the selected product
def get_product_questions(product_name, language):
    if language == "Hindi":
        products = Product.query.filter_by(name=product_name).with_entities(Product.question_hindi, Product.answer_hindi).all()
    else:
        products = Product.query.filter_by(name=product_name).with_entities(Product.question_english, Product.answer_english).all()

    product_questions = []
    product_answers = []
    for question, answer in products:
        product_questions.append(question)
        product_answers.append(answer)

    return product_questions, product_answers # Returning two lists, one for questions, one for answers


# Function to get the correct answer for a given question
def get_correct_answer(product_name, current_question, language):
    if language == "Hindi":
        correct_answer_row = Product.query.filter_by(name=product_name, question_hindi=current_question).first()
        if correct_answer_row:
            return correct_answer_row.answer_hindi
    else:
        correct_answer_row = Product.query.filter_by(name=product_name, question_english=current_question).first()
        if correct_answer_row:
            return correct_answer_row.answer_english
    return None

# Main conversation handler
@reflect_bp.route('/conversation/<string:product_name>', methods=['POST'])
@login_required
async def manage_conversation(product_name):
    try:
        # Step 1: Get the action and other parameters from the request
        action = request.json.get('action', 'answer')  # Default action is to provide feedback for the answer
        language = request.json.get('language', session.get('language', 'Hindi'))
        session['language'] = language
        user_answer = request.json.get('user_transcript')

        # Step 2: Check if it's a new conversation
        conversation_id = session.get('conversation_id')

        # Log incoming request details
        current_app.logger.info(f"Received conversation ID: {conversation_id}, User Answer: {user_answer}, Action: {action}")

        if not conversation_id:
            # Initialize a new conversation
            conversation_id = initialize_refer_conversation(current_user.id, product_name)
            session['conversation_id'] = conversation_id
            session['score'] = 0
            session['questions_asked'] = 0
            session['correct_answers'] = 0

            # Fetch both questions and their corresponding answers based on language
            product_questions, product_answers = get_product_questions(product_name, language)

            if not product_questions:
                return jsonify({"error": NO_QUESTIONS_FOUND_MSG}), 404

            # Shuffle the questions while keeping answers paired
            random.shuffle(product_questions)

            # Store both questions and answers in session for easy retrieval
            session['shuffled_questions'] = [{'question': q[0], 'answer': q[1]} for q in product_questions]
            session['total_questions'] = min(10, len(session['shuffled_questions']))  # Set total to 10

            # Get the first question
            current_question = session['shuffled_questions'][0]['question']
            session['current_question'] = current_question
            session['questions_asked'] = 1
            session.modified = True

            # AI Coach greeting and context setting based on selected language
            greetings = {
                "Hindi": [
                    f"नमस्ते! मैं आज आपका कोच हूँ। हम साथ में {product_name} के बारे में आपके ज्ञान को समझेंगे। यहाँ आपका पहला प्रश्न है।",
                ],
                "English": [
                    f"Hello! I'm your coach today. Let’s explore your knowledge of {product_name}. Here’s your first question."
                ]
            }

            # Randomly select a greeting
            coach_greeting = random.choice(greetings[language])
            question_prompt = current_question

            # Synthesize and return the first question with audio
            conversation_context = f"{coach_greeting}\n{question_prompt}"

            current_app.logger.info(f"Generating speech for: {conversation_context}")
            audio_file_name = await synthesize_speech(conversation_context, language)
            current_app.logger.info(f"Audio file generated: {audio_file_name}")

            if not audio_file_name:
                current_app.logger.error(AUDIO_GENERATION_ERROR_MSG)
                return jsonify({"error": AUDIO_GENERATION_ERROR_MSG}), 500

            return jsonify({
                "text": current_question,
                "audio": f"/static/{audio_file_name}",
                "conversation_id": conversation_id
            })

        # Step 3: Handle actions for an ongoing conversation
        if action == 'answer':
            # Provide feedback for the current answer
            if 'shuffled_questions' not in session or 'questions_asked' not in session:
                current_app.logger.error("Session data missing: shuffled_questions or questions_asked")
                return jsonify({"error": "Session data is missing."}), 400

            current_question_index = session['questions_asked'] - 1

            # Ensure the index is within bounds
            if current_question_index >= len(session['shuffled_questions']):
                return jsonify({"error": "No more questions available."}), 400

            current_qa_pair = session['shuffled_questions'][current_question_index]
            correct_answer = current_qa_pair['answer']

            current_app.logger.info(
                f"Current question index: {current_question_index}, Correct Answer: {correct_answer}")

            prompt = [
                SystemMessage(
                    content=f"""
                        Your task is to check the user's answer and provide feedback on it.
                        User's Answer: "{user_answer}"
                        Correct Answer: "{correct_answer}"
                    """
                ),
                HumanMessage(content="Please provide feedback on the answer.")
            ]

            feedback_response = await llm.chat(prompt)
            feedback_text = feedback_response.content.strip()
            session.modified = True  # Mark session as modified

            # Synthesize feedback audio
            feedback_audio_filename = await synthesize_speech(feedback_text, language)

            if not feedback_audio_filename:
                current_app.logger.error(FINAL_FEEDBACK_ERROR_MSG)
                return jsonify({"error": FINAL_FEEDBACK_ERROR_MSG}), 500

            # Update score and question count based on the answer
            session['score'] += 1 if user_answer.strip().lower() == correct_answer.strip().lower() else 0
            session['questions_asked'] += 1
            session['correct_answers'] += 1 if user_answer.strip().lower() == correct_answer.strip().lower() else 0

            # Check if more questions are left
            if session['questions_asked'] < session['total_questions']:
                next_question = session['shuffled_questions'][session['questions_asked']]['question']
                session['current_question'] = next_question

                question_prompt = f"Next question is: {next_question}"
                question_context = f"Next question is: {next_question}"
                
                # Synthesize the next question audio
                next_audio_file_name = await synthesize_speech(question_context, language)

                if not next_audio_file_name:
                    current_app.logger.error(AUDIO_GENERATION_ERROR_MSG)
                    return jsonify({"error": AUDIO_GENERATION_ERROR_MSG}), 500

                return jsonify({
                    "feedback": feedback_text,
                    "audio": f"/static/{feedback_audio_filename}",
                    "next_question": next_question,
                    "next_audio": f"/static/{next_audio_file_name}"
                })
            else:
                return jsonify({
                    "feedback": feedback_text,
                    "final_score": session['score']
                })
    except Exception as e:
        current_app.logger.error(f"Error in manage_conversation: {str(e)}")
        return jsonify({"error": "An unexpected error occurred. Please try again later."}), 500

# Function to synthesize speech from text
async def synthesize_speech(text, language):
    try:
        voice = VOICE_MAPPING["Male"] if language == "Hindi" else VOICE_MAPPING["Female"]
        audio_file_name = f"{text[:10]}_audio.wav"  # Example file naming strategy
        # Actual audio synthesis logic would go here...
        return audio_file_name  # Simulating successful synthesis
    except Exception as e:
        current_app.logger.error(f"Audio synthesis failed: {str(e)}")
        return None

# Example function to add messages to the conversation database
def add_message(conversation_id, sender, content):
    # Placeholder function to demonstrate message addition
    pass

# Example function to initialize a conversation
def initialize_refer_conversation(user_id, product_name):
    # Placeholder for initializing a conversation and returning its ID
    return 1

# Example function to generate feedback for the conversation
async def generate_refer_feedback(conversation):
    # Placeholder function for generating feedback based on a conversation
    return "Great job! Keep up the good work."





