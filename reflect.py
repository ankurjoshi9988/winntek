from flask import request, jsonify, session
from flask_login import login_required, current_user
import random, uuid
import azure.cognitiveservices.speech as speechsdk
import asyncio
from models import Product, ReferConversation, Conversation  # Adjust based on your project structure
from flask import Blueprint
import numpy as np
import os
import google.generativeai as genai
from langchain_community.vectorstores import FAISS
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain.prompts import PromptTemplate
from langchain.chains.question_answering import load_qa_chain
from langchain_google_genai import ChatGoogleGenerativeAI
from flask import current_app
from conversation_service import start_conversation, add_message, close_conversation, get_past_conversations, start_refer_conversation, add_refer_message, generate_refer_feedback
from extensions import login_manager, csrf, mail, oauth, db
reflect_bp = Blueprint('reflect', __name__)

api_key=os.environ['GOOGLE_API_KEY']
genai.configure(api_key=api_key)
azure_subscription_key = os.getenv("AZURE_SUBSCRIPTION_KEY")
azure_region = os.getenv("AZURE_REGION")
llm = ChatGoogleGenerativeAI(model="gemini-pro", convert_system_message_to_human=True, temperature=0.8)


# Define voice mappings for male and female personas
VOICE_MAPPING = {
    "Male": "hi-IN-MadhurNeural",
    "Female": "hi-IN-SwaraNeural"
}


# Add a message to the conversation
@reflect_bp.route('/add_refer_message', methods=['POST'])
@login_required
def add_refer_message():
    print(session.get('_csrf_token'))
    data = request.json
    current_app.logger.info(f"Request Data: {data}")  # Log the incoming request

    conversation_id = data.get('conversation_id')
    sender = data.get('sender')
    content = data.get('content')

    if not conversation_id or not sender or not content:
        current_app.logger.error(f"Invalid request: conversation_id={conversation_id}, sender={sender}, content={content}")
        return jsonify({'error': 'Invalid request'}), 400

    add_message(conversation_id, sender, content)
    return jsonify({'status': 'Message added'}), 200



# Close the conversation and generate feedback
@reflect_bp.route('/close_refer_conversation', methods=['POST'])
@login_required
async def close_refer_conversation_route():
    data = request.json
    conversation_id = data.get('conversation_id')

    conversation = ReferConversation.query.get(conversation_id)
    if not conversation:
        return jsonify({'error': 'Conversation not found'}), 404

    feedback_content = await generate_refer_feedback(conversation)  # Assuming this generates feedback based on user performance
    return jsonify({'status': 'Conversation closed', 'feedback': feedback_content})


# Load products for selection
@reflect_bp.route('/load-products', methods=['GET'])
@login_required
def load_products():
    try:
        # Fetch distinct product names
        products = db.session.query(Product.name).distinct().all()
        product_list = [{"name": product[0]} for product in products]
        return jsonify({"products": product_list})
    except Exception as e:
        current_app.logger.error(f"Error loading products: {e}")
        return jsonify({"error": "Failed to load products"}), 500

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

    return product_questions, product_answers  # Returning two lists, one for questions, one for answers


def get_correct_answer(product_name, current_question, language):
    """Fetch the correct answer for the given product and question."""
    if language == "Hindi":
        correct_answer_row = Product.query.filter_by(name=product_name, question_hindi=current_question).first()
        if correct_answer_row:
            return correct_answer_row.answer_hindi
    else:
        correct_answer_row = Product.query.filter_by(name=product_name, question_english=current_question).first()
        if correct_answer_row:
            return correct_answer_row.answer_english
    return None

@reflect_bp.route('/start_refer_conversation/<string:product_name>', methods=['POST'])
@login_required
async def start_refer_conversation(product_name):
    try:
        # Initialize conversation settings
        conversation_id = session.get('conversation_id')
        session['product_name'] = product_name

        if not conversation_id:
            conversation_id = initialize_refer_conversation(current_user.id, product_name)
            session['conversation_id'] = conversation_id
            session['score'] = 0
            session['questions_asked'] = 0
            session['correct_answers'] = 0
            session['shuffled_questions'] = []
            session.modified = True

        language = request.json.get('language', session.get('language', 'Hindi'))
        session['language'] = language

        # Fetch both questions and their corresponding answers
        if language == "Hindi":
            product_questions = Product.query.filter_by(name=product_name).with_entities(Product.question_hindi, Product.answer_hindi).all()
        else:
            product_questions = Product.query.filter_by(name=product_name).with_entities(Product.question_english, Product.answer_english).all()

        if not product_questions:
            return jsonify({"error": "No questions found for the selected product."}), 404

        # Shuffle the questions while keeping answers paired
        random.shuffle(product_questions)

        # Store both questions and answers in session for easy retrieval
        session['shuffled_questions'] = [{'question': q[0], 'answer': q[1]} for q in product_questions]
        session['total_questions'] = min(10, len(session['shuffled_questions']))  # Set the total to 10

        # Get the first question
        current_question = session['shuffled_questions'][0]['question']
        session['current_question'] = current_question
        session['questions_asked'] = 1
        session.modified = True


        # AI Coach greeting and context setting based on selected language
        if session['language'] == "Hindi":
            hindi_greetings = [
                f"नमस्ते! मैं आज आपका कोच हूँ। हम साथ में {product_name} के बारे में आपके ज्ञान को समझेंगे। कोई चिंता की बात नहीं, मैं यहाँ आपकी मदद के लिए हूँ। यह रहा आपका पहला प्रश्न।",
                f"नमस्कार! आज हम {product_name} के बारे में आपकी समझ का परीक्षण करेंगे। मैं आपके साथ हूँ और हर कदम पर आपका मार्गदर्शन करूंगा। तो, शुरू करते हैं। यहाँ पहला प्रश्न है।",
                f"आपका स्वागत है! मैं आपका कोच हूँ और आज हम {product_name} से जुड़ी कुछ बातें जानेंगे। आप तैयार हैं? तो चलिए शुरू करते हैं, यह रहा पहला सवाल।",
                f"नमस्ते! मैं यहाँ हूँ आपकी मदद के लिए, ताकि हम मिलकर {product_name} के बारे में आपकी जानकारी को सुधारें। कोई भी संकोच मत कीजिए, यह रहा आपका पहला प्रश्न।",
                f"नमस्कार! आज हम {product_name} पर आधारित आपके ज्ञान का मूल्यांकन करेंगे। चिंता मत कीजिए, मैं आपके साथ हूँ। शुरू करते हैं, यह रहा पहला सवाल।"
            ]
            # Randomly select a greeting
            coach_greeting = random.choice(hindi_greetings)
            question_prompt = current_question

        else:
            english_greetings = [
                f"Hello! I'm your coach today. Let’s explore your knowledge of {product_name}. Don’t worry, I’m here to guide you. Here’s your first question.",
                f"Welcome! I'm here to help you test your understanding of {product_name}. Ready? Let’s dive in. Here's the first question for you.",
                f"Hello! Let’s work together to assess your knowledge of {product_name}. No need to worry, I’ll be right here to assist. Here's your first question.",
                f"Greetings! I'm your coach today, and we’ll be covering {product_name}. Don’t worry, I’ll guide you through it step by step. Let's begin with the first question.",
                f"Hi! I’m here to guide you through a quick test of your knowledge on {product_name}. I’ll be with you throughout. Here’s your first question."
            ]
            # Randomly select a greeting
            coach_greeting = random.choice(english_greetings)
            question_prompt = current_question

        # Full conversational context with the coach-like behavior
        conversation_context = f"{coach_greeting}\n{question_prompt}"

        # Choose the correct Azure voice for Hindi/English
        selected_voice = VOICE_MAPPING["Male"] if language == "Hindi" else "en-IN-PrabhatNeural"
        speech_config = speechsdk.SpeechConfig(subscription=azure_subscription_key, region=azure_region)
        speech_config.speech_synthesis_voice_name = selected_voice

        # Synthesize speech for the conversational context
        audio_file_name = str(uuid.uuid4()) + ".mp3"
        audio_config = speechsdk.audio.AudioOutputConfig(filename=f"static/{audio_file_name}")
        speech_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)

        # Synthesize the speech
        result = await asyncio.to_thread(speech_synthesizer.speak_text_async(conversation_context).get)

        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            print(f"Speech synthesized for text [{conversation_context}]")
        elif result.reason == speechsdk.ResultReason.Canceled:
            print(f"Speech synthesis canceled: {result.cancellation_details.reason}")

        # Return conversation context and audio file name
        return jsonify({
            "text": question_prompt,
            "audio": f"/static/{audio_file_name}",
            "conversation_id": conversation_id
        })

    except Exception as e:
        current_app.logger.error(f"Error in start_refer_conversation: {e}")
        return jsonify({"error": str(e)}), 500


# Step 2: Validate user responses
@reflect_bp.route('/validate_answer', methods=['POST'])
@login_required
async def validate_answer():
    print(f"Route /validate_answer called")
    try:
        data = request.get_json()
        conversation_id = session.get('conversation_id')
        user_answer = data.get('message', '').strip().lower()

        # Ensure we have valid data to proceed
        if 'shuffled_questions' not in session or 'questions_asked' not in session:
            current_app.logger.error("No shuffled questions or questions_asked in session.")
            return jsonify({"error": "Session data is missing."}), 400

        # Get the current question-answer pair
        current_question_index = session['questions_asked'] - 1
        current_qa_pair = session['shuffled_questions'][current_question_index]

        # Fetch the correct answer for the current question
        correct_answer = current_qa_pair['answer']
        print("correct_answer: ", correct_answer)
        # Modify the prompt to explicitly ask for answer evaluation
        if session['language'] == 'Hindi':
            prompt = f"""
            आप एक कोच हैं और आपको एक छात्र का उत्तर मूल्यांकन करना है। कोलोक्विल हिंदी का इस्तेमाल करें। जहां अंग्रेजी शब्द बोलना पड़े वहां बोलो।
            user का उत्तर: "{user_answer}"
            सही उत्तर: "{correct_answer}"
            क्या user का उत्तर सही है? उत्तर के समानता और संदर्भ को ध्यान में रखते हुए एक कोच की तरह उत्तर दें। कृपया 70% semantic similarity (अर्थ की समानता) के आधार पर सही, अधूरा, या गलत उत्तर दें। अगर उत्तर सही है लेकिन थोड़ा अलग है, तो उसे सही मानें और क्यों यह उत्तर सही, अधूरा या गलत है, इसका संक्षिप्त विवरण दें। बातचीत में समानता के % उल्लेख न करें।
            """

        else:
            prompt = f"""
            You are a coach evaluating a student's response.
            user answer: "{user_answer}"
            Correct answer: "{correct_answer}"
            Is the student's answer correct? Please evaluate based on semantic similarity and context. Provide either 'Correct', 'Incomplete', or 'Incorrect' as your evaluation. If the user's answer is semantically correct but phrased differently, consider it correct and provide a brief explanation of why the answer is correct, incomplete, or incorrect. do not mention % of similarity in conversation.
            """

        # AI LLM call to generate a human-like conversational response
        response = await asyncio.to_thread(llm.invoke, prompt)
        coach_response = response.content

        # Check if the response contains feedback indicating the answer is correct
        coach_response_lower = coach_response.lower()

        # Logging to track the condition evaluation
        print(f"Coach response: {coach_response_lower}")

        # Check if the response contains feedback indicating the answer is correct, incomplete, or incorrect
        if "correct" in coach_response.lower() and "incomplete" not in coach_response.lower() and "अधूरा" not in coach_response and "गलत" not in coach_response:
            session['correct_answers'] += 1  # Increment correct answers if the LLM indicates it's fully correct
            print(f"Correct answers so far: {session['correct_answers']}")
        elif "incomplete" in coach_response.lower() or "अधूरा" in coach_response:  # Handle incomplete answers
            print("The answer was incomplete.")
        else:
            print("The answer was marked incorrect.")

        session['questions_asked'] += 1
        """
        # Check if we've reached the limit of 10 questions
        if session['questions_asked'] > 10 or session['questions_asked'] > session['total_questions']:
            final_score = session['correct_answers']
            feedback = generate_feedback(final_score, session['total_questions']+1)  # Provide feedback after 10 questions or when all are asked
            print("feedback: ", feedback)
            response = jsonify({"feedback_text": feedback, "conversation_id": conversation_id})
            # Clear the session after sending the response
            session.clear()  # End the quiz after feedback is generated

            return response
            """
        # Move to the next question
        next_question_index = session['questions_asked'] - 1
        next_question = session['shuffled_questions'][next_question_index]['question']

        # Store the next question in the session
        session['current_question'] = next_question

        # Full conversational context with the coach-like behavior
        conversation_context = f"{coach_response}\n{next_question}"

        language = request.json.get('language', session.get('language', 'Hindi'))
        session['language'] = language

        # Choose the correct Azure voice for Hindi/English
        selected_voice = VOICE_MAPPING["Male"] if language == "Hindi" else "en-IN-PrabhatNeural"
        speech_config = speechsdk.SpeechConfig(subscription=azure_subscription_key, region=azure_region)
        speech_config.speech_synthesis_voice_name = selected_voice

        # Synthesize speech for the conversational context
        audio_file_name = str(uuid.uuid4()) + ".mp3"
        audio_config = speechsdk.audio.AudioOutputConfig(filename=f"static/{audio_file_name}")
        speech_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)

        # Synthesize feedback response
        feedback_audio_file_name = str(uuid.uuid4()) + ".mp3"
        feedback_audio_config = speechsdk.audio.AudioOutputConfig(filename=f"static/{feedback_audio_file_name}")
        feedback_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=feedback_audio_config)
        feedback_result = await asyncio.to_thread(feedback_synthesizer.speak_text_async(coach_response).get)

        if feedback_result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            print(f"Feedback speech synthesized for text [{coach_response}]")
        elif feedback_result.reason == speechsdk.ResultReason.Canceled:
            print(f"Feedback speech synthesis canceled: {feedback_result.cancellation_details.reason}")

        # Synthesize the next question
        next_question_audio_file_name = str(uuid.uuid4()) + ".mp3"
        next_question_prompt = f"Here's your next question: {next_question}"
        next_question_audio_config = speechsdk.audio.AudioOutputConfig(filename=f"static/{next_question_audio_file_name}")
        next_question_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=next_question_audio_config)
        next_question_result = await asyncio.to_thread(next_question_synthesizer.speak_text_async(next_question_prompt).get)

        if next_question_result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            print(f"Next question speech synthesized for question [{next_question_prompt}]")
        elif next_question_result.reason == speechsdk.ResultReason.Canceled:
            print(f"Next question speech synthesis canceled: {next_question_result.cancellation_details.reason}")

        return jsonify({
            "answer_feedback_text": correct_answer,
            "feedback_audio": f"/static/{feedback_audio_file_name}",  # Feedback audio
            "next_question_text": "\n" + next_question,  # Show the next question
            "next_question_audio": f"/static/{next_question_audio_file_name}",  # Next question audio
            "conversation_id": conversation_id
        })

    except IndexError as e:
        current_app.logger.error(f"IndexError in validate_answer: {str(e)}")
        return jsonify({"error": "Index out of range"}), 500

    except Exception as e:
        current_app.logger.error(f"Error in validate_answer: {str(e)}")
        return jsonify({"error": str(e)}), 500

def get_conversational_chain():
    prompt_template = """
    संदर्भ (Context):\n{context}\n
    प्रश्न (Question):\n{question}\n
    उत्तर (Answer):
    Provide a clear and well-structured response. If the answer is not available, simply state, "उत्तर संदर्भ में उपलब्ध नहीं है" (answer is not available in the context).
    """

    model = ChatGoogleGenerativeAI(model="gemini-pro", temperature=0.3)
    prompt = PromptTemplate(template=prompt_template, input_variables=["context", "question"])
    chain = load_qa_chain(model, chain_type="stuff", prompt=prompt)

    return chain


# Validate user answer using RAG-based semantic similarity
def cosine_similarity(vec1, vec2):
    return np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))

# Updated RAG validation function using embeddings
def validate_user_answer_with_rag(user_answer, correct_answer):
    """
    This function uses RAG to validate the user's answer based on similarity matching.
    """

    # Load embeddings and FAISS index
    embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")
    vector_store = FAISS.load_local("faiss_index", embeddings, allow_dangerous_deserialization=True)

    # Get embeddings for user answer and correct answer
    user_answer_embedding = embeddings.embed_documents([user_answer])[0]
    correct_answer_embedding = embeddings.embed_documents([correct_answer])[0]

    # Calculate similarity between user answer and correct answer embeddings
    similarity_score = cosine_similarity(user_answer_embedding, correct_answer_embedding)

    # Set a threshold for determining correctness (e.g., 0.8 for 80% similarity)
    threshold = 0.8
    is_correct = similarity_score >= threshold
    print("is_correct: ",is_correct)
    return is_correct, similarity_score



def get_vector_store(product_descriptions):
    embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")
    vector_store = FAISS.from_texts(product_descriptions, embedding=embeddings)
    vector_store.save_local("faiss_index")
    return vector_store




# Helper function to generate feedback based on the score
def generate_feedback(correct_answers, total_questions):
    score_percentage = (correct_answers / total_questions) * 100
    score = f"Score: {correct_answers}/{total_questions} ({score_percentage:.2f}%)"

    if score_percentage >= 90:
        category = "Expert"
        feedback_text = f"Excellent job! You have a deep understanding of the product and effectively communicated its benefits. Keep up the great work!\n{score}"
    elif score_percentage >= 60:
        category = "Proficient"
        feedback_text = f"Good effort! You have a solid grasp of the product but there are some areas where you can improve. Focus on refining your knowledge.\n{score}"
    elif score_percentage >= 25:
        category = "Competent"
        feedback_text = f"You’re on the right track, but there’s more to learn. Review the product details and practice to enhance your knowledge.\n{score}"
    else:
        category = "Beginner"
        feedback_text = f"You need to learn and practice more. Take time to study the product thoroughly and seek guidance to improve your understanding.\n{score}"

    return f"Category: {category}\nFeedback: {feedback_text}"






# Initialize a new conversation in the database
def initialize_refer_conversation(user_id, product_name):
    conversation = Conversation(user_id=user_id, persona=product_name)
    db.session.add(conversation)
    db.session.commit()
    return conversation.id

