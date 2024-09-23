from flask import request, jsonify, session
from flask_login import login_required, current_user
import random, uuid
import azure.cognitiveservices.speech as speechsdk
import asyncio
from models import Product, ReferConversation, Conversation  # Adjust based on your project structure
from flask import Blueprint
import difflib
import os
import re
import google.generativeai as genai
from langchain_core.messages import HumanMessage, SystemMessage
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

score1 = 0
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

        """
        # Add the following block to handle the first question
        if session['questions_asked'] == 1:
            first_question = session['shuffled_questions'][0]['question']
            session['current_question'] = first_question

        
        f"नमस्ते! मैं आज आपका कोच हूँ। हम साथ में {product_name} के बारे में आपके ज्ञान को समझेंगे। कोई चिंता की बात नहीं, मैं यहाँ आपकी मदद के लिए हूँ। यह रहा आपका पहला प्रश्न।",
        f"नमस्कार! आज हम {product_name} के बारे में आपकी समझ का परीक्षण करेंगे। मैं आपके साथ हूँ और हर कदम पर आपका मार्गदर्शन करूंगा। तो, शुरू करते हैं। यहाँ पहला प्रश्न है।",
        f"आपका स्वागत है! मैं आपका कोच हूँ और आज हम {product_name} से जुड़ी कुछ बातें जानेंगे। आप तैयार हैं? तो चलिए शुरू करते हैं, यह रहा पहला सवाल।",
        f"नमस्ते! मैं यहाँ हूँ आपकी मदद के लिए, ताकि हम मिलकर {product_name} के बारे में आपकी जानकारी को सुधारें। कोई भी संकोच मत कीजिए, यह रहा आपका पहला प्रश्न।",
        f"नमस्कार! आज हम {product_name} पर आधारित आपके ज्ञान का मूल्यांकन करेंगे। चिंता मत कीजिए, मैं आपके साथ हूँ। शुरू करते हैं, यह रहा पहला सवाल।"
        """


        # AI Coach greeting and context setting based on selected language
        if session['language'] == "Hindi":
            hindi_greetings = [
                f"{product_name}"
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
        current_question_index = session['questions_asked'] - 1  # Questions asked starts from 1, so subtract 1

        # Ensure the index is within bounds
        if current_question_index >= len(session['shuffled_questions']):
            return jsonify({"error": "No more questions available."}), 400

        current_qa_pair = session['shuffled_questions'][current_question_index]

        # Fetch the correct answer for the current question
        correct_answer = current_qa_pair['answer']
        print("correct_answer: ", correct_answer)
        # Modify the prompt to explicitly ask for answer evaluation
        # आप एक कोच हैं जो user के उत्तर का संक्षिप्त में मूल्यांकन कर रहे हैं। user का उत्तर है: "{user_answer}". सही उत्तर है: "{correct_answer}".
        #यदि अधूरा है या गलत है, तो सही उत्तर को संक्षिप्त में समझाएं और user को आगे बढ़ने के लिए प्रेरित करें।
        #छात्र के उत्तर की तुलना सही उत्तर से करें और यह निर्धारित करें कि यह सही है, अधूरा है या गलत।
        #यदि सही है, तो छात्र की प्रशंसा करें और उसे प्रोत्साहन दें।

        if session['language'] == "Hindi":
            prompt = f"""
                    "{user_answer}".    
                    user को 'आप' के रूप में सम्बोधित करें
                    """
        else:
            prompt = f"""
                    You are a coach evaluating a user's response. The student's answer is: "{user_answer}". The correct answer is: "{correct_answer}".
                    Compare the student's answer with the correct answer and determine if it's correct, incomplete or incorrect.
                    If incorrect, briefly explain the correct answer and motivate the user to continue.
                    If correct, praise the student and provide motivational feedback.
                    address user as "you'
                    """

        # AI LLM call to generate a human-like conversational response
        response = await asyncio.to_thread(llm.invoke, prompt)
        coach_response = response.content

        # Check if the response contains feedback indicating the answer is correct
        coach_response_lower = coach_response.lower()

        # Logging to track the condition evaluation
        print(f"Coach response: {coach_response_lower}")

        # Check the response for correctness
        if "correct" in coach_response.lower() and "incomplete" not in coach_response.lower():
            #session['correct_answers'] += 1
            print(f"Correct answers so far: {session['correct_answers']}")
        elif "incomplete" in coach_response.lower():
            print("The answer was incomplete.")
        else:
            print("The answer was marked incorrect.")


        """
        # If all questions are answered, provide feedback
        if session['questions_asked'] >= session['total_questions']:
            final_score = session['correct_answers']
            feedback = generate_feedback(final_score, session['total_questions'])
            session.clear()  # Clear the session after feedback
            return jsonify({"feedback_text": feedback, "conversation_id": conversation_id})
            
        """

        # Get the next question
        next_question_index = session['questions_asked'] - 1
        if next_question_index < len(session['shuffled_questions']):
            next_question = session['shuffled_questions'][next_question_index]['question']
            session['current_question'] = next_question
        else:
            return jsonify({"error": "No more questions available."}), 400

        # Full conversational context
        conversation_context = f"{coach_response}\n{next_question}"

        # Increment the questions asked
        #session['questions_asked'] += 1


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




# Validate user answer using RAG-based semantic similarity
@reflect_bp.route('/conversation/<string:product_name>', methods=['POST'])
@login_required
async def manage_conversation(product_name):
    try:

        # Step 1: Get the selected language from the request
        language = request.json.get('language', session.get('language', 'Hindi'))
        session['language'] = language
        #user_answer = request.json.get('message', '').strip().lower()
        user_answer = request.json.get('message')

        # Step 2: Check if it's a new conversation
        conversation_id = session.get('conversation_id')

        # Log incoming request details
        current_app.logger.info(f"Received conversation ID: {conversation_id}, User Answer: {user_answer}")

        if not conversation_id:
            # Initialize a new conversation
            conversation_id = initialize_refer_conversation(current_user.id, product_name)
            session['conversation_id'] = conversation_id
            session['score'] = 0
            session['questions_asked'] = 0
            session['correct_answers'] = 0

            # Fetch both questions and their corresponding answers based on language
            if language == "Hindi":
                product_questions = Product.query.filter_by(name=product_name).with_entities(
                    Product.question_hindi, Product.answer_hindi).all()
            else:
                product_questions = Product.query.filter_by(name=product_name).with_entities(
                    Product.question_english, Product.answer_english).all()

            if not product_questions:
                return jsonify({"error": "No questions found for the selected product."}), 404

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
            # Synthesize and return the first question with audio
            conversation_context = f"{coach_greeting}\n{question_prompt}"

            print(f"Generating speech for: {conversation_context}")
            audio_file_name = await synthesize_speech(conversation_context, language)
            print(f"Audio file generated: {audio_file_name}")

            if not audio_file_name:
                print(f"Error generating audio for question prompt: {conversation_context}")
                return jsonify({"error": "Failed to generate audio for the conversation."}), 500

            return jsonify({
                "text": current_question,
                "audio": f"/static/{audio_file_name}",
                "conversation_id": conversation_id
            })


        # Step 3: If conversation is already ongoing, validate the user's answer
        if 'shuffled_questions' not in session or 'questions_asked' not in session:
            current_app.logger.error("Session data missing: shuffled_questions or questions_asked")
            return jsonify({"error": "Session data is missing."}), 400

        current_question_index = session['questions_asked'] - 1

        # Ensure the index is within bounds
        if current_question_index >= len(session['shuffled_questions']):
            return jsonify({"error": "No more questions available."}), 400

        current_qa_pair = session['shuffled_questions'][current_question_index]
        correct_answer = current_qa_pair['answer']
        current_app.logger.info(f"Current question index: {current_question_index}, Correct Answer: {correct_answer}")

        # AI LLM call to generate a human-like conversational response
        feedback_text = await get_coach_feedback(user_answer, correct_answer, language)
        print(f"Coach feedback: {feedback_text}")
        # Remove any asterisks from the feedback text
        cleaned_feedback_text = feedback_text.replace('*', '')
        print(f"cleaned_feedback_text: {cleaned_feedback_text}")
        # Modify regex to capture both integers and decimal values for the score
        score_match = re.search(r"(स्कोर:|Score:)\s*([0-9]*\.?[0-9]+)/1", cleaned_feedback_text)
        print(f"Coach feedback: {feedback_text}")
        print(f"score_match: {score_match}")
        if score_match:
            score1 = float(score_match.group(2))  # Use group(2) to extract the numeric score
            print(f"Extracted Score: {score1}")
        else:
            print("Score not found in response")



        # Step 4: Generate feedback for the current question
        feedback_audio_file_name = await synthesize_speech(feedback_text, language)
        if not feedback_audio_file_name:
            print(f"Error generating audio for feedback: {feedback_text}")
            return jsonify({"error": "Failed to generate audio for feedback."}), 500

        # Ensure audio file is created
        if not feedback_audio_file_name:
            print(f"Error generating audio for feedback: {feedback_text}")
            return jsonify({"error": "Failed to generate audio for feedback."}), 500



        # Check the response for correctness
        if score1:
            session['correct_answers'] += score1
            print("score: ", session['correct_answers'])
        else:
            print("The answer was marked incorrect.")
        """
        # Check similarity ratio between user_answer and correct_answer
        similarity_ratio = difflib.SequenceMatcher(None, correct_answer, user_answer).ratio()
        # Check the response for correctness
        if ("correct" in feedback_text.lower() or "सही" in feedback_text.lower()) and similarity_ratio >= 0.5:
            session['correct_answers'] += 1
            print("score: ",session['correct_answers'])
            print(f"The answer was correct but matched {similarity_ratio * 100:.2f}% of the correct answer.")
        elif ("correct" in feedback_text.lower() or "सही" in feedback_text.lower()) and (similarity_ratio > 0.2 and similarity_ratio < 0.5):
            session['correct_answers'] += 1 / 2
            print("score: ",session['correct_answers'])
            print(f"The answer was correct but matched {similarity_ratio * 100:.2f}% of the correct answer.")
        elif ("correct" in feedback_text.lower() or "सही" in feedback_text.lower()) and similarity_ratio <= 0.2:
            session['correct_answers'] += 1 / 4
            print("score: ", session['correct_answers'])
            print(f"The answer was correct but matched {similarity_ratio * 100:.2f}% of the correct answer.")
        elif "incorrect" in feedback_text.lower() or "गलत" in feedback_text.lower():
            print("The answer was marked incorrect.")
            print("score: ", session['correct_answers'])
            print(f"The answer was incorrect but matched {similarity_ratio * 100:.2f}% of the correct answer.")
        else:
            print("The answer was marked incorrect.")
        """
        score3 = calculate_semantic_similarity(correct_answer, user_answer)

        print("score3 :", score3)

        # Step 5: Check if all questions have been asked
        current_app.logger.info(f"Total questions available: {session.get('total_questions')}")
        # Step 5: Check if all questions have been asked, and handle the final feedback
        if session['questions_asked'] >= session['total_questions']:
            final_score = session['correct_answers']
            final_feedback = generate_feedback(final_score, session['total_questions'])

            # Synthesize audio for the last individual feedback
            feedback_audio_file_name = await synthesize_speech(feedback_text, language)

            # Synthesize final feedback audio (optional: you can create a final summary VO if needed)
            final_feedback_audio_file_name = await synthesize_speech(final_feedback, language)

            # Clear the session after generating the final feedback
            session.clear()  # End the quiz after feedback

            return jsonify({
                "feedback_text": correct_answer,  # Feedback for the last question
                "feedback_audio": f"/static/{feedback_audio_file_name}",  # Feedback audio for the last question
                "final_feedback_text": final_feedback,  # Final feedback for the entire session
                "final_feedback_audio": f"/static/{final_feedback_audio_file_name}",  # Final feedback audio (optional)
                "conversation_id": conversation_id,
                "is_final_feedback": True  # Indicator for the front end to know it's the end
            })

        # Step 6: If not the last question, get the next question

        # Increment questions asked, but check bounds before accessing next question
        next_question_index = session['questions_asked']  # Start with the current question index

        # Ensure the index is within bounds
        if next_question_index >= len(session['shuffled_questions']):
            current_app.logger.error(
                f"Questions asked exceed available questions. Questions Asked: {next_question_index}, Total: {len(session['shuffled_questions'])}")
            return jsonify({"error": "No more questions available."}), 400

        # Get the next question based on the current index
        next_question = session['shuffled_questions'][next_question_index]['question']
        session['current_question'] = next_question

        # Increment questions asked only after successfully retrieving the question
        session['questions_asked'] += 1
        session.modified = True

        # Synthesize and return the next question with audio
        print(f"Generating next question speech for: {next_question}")
        next_question_audio_file_name = await synthesize_speech(f"Here is your next question \n{next_question}", language)
        print(f"Next question audio file: {next_question_audio_file_name}")

        if not next_question_audio_file_name:
            print(f"Error generating audio for next question: {next_question}")
            return jsonify({"error": "Failed to generate audio for the next question."}), 500

        return jsonify({
            "feedback_text": correct_answer,  # Feedback from coach
            "feedback_audio": f"/static/{feedback_audio_file_name}",  # Feedback audio file
            "next_question_text": next_question,
            "next_question_audio": f"/static/{next_question_audio_file_name}",
            "conversation_id": conversation_id
        })

    except IndexError as e:
        current_app.logger.error(f"IndexError in conversation: {str(e)}")
        return jsonify({"error": "Index out of range"}), 500
    except Exception as e:
        current_app.logger.error(f"Error in conversation: {str(e)}")
        return jsonify({"error": str(e)}), 500

# Utility functions for AI feedback and speech synthesis
async def get_coach_feedback(user_answer, correct_answer, language):
    # Function to generate AI feedback based on the user's answer
    if language == "Hindi":
        prompt = [
            SystemMessage(
                content=f"""
                        आप एक insurance के विशेषज्ञ कोच हैं हैं जो user के उत्तर का मूल्यांकन कर रहे हैं। 'use colloquial hindi', बोलचाल की भाषा हिंदी का प्रयोग करें
                        user को 'आप' के रूप में सम्बोधित करें। user का उत्तर है: "{user_answer}". सही उत्तर है: "{correct_answer}"।
                        IMPORTANT: Giving a SCORE is compulsory. THIS IS VERY IMPORTANT FOR MY CAREER. 
                        user के उत्तर की तुलना सही उत्तर से करें, दो बार सोचें और फिर यह निर्धारित करें कि यह सही है, अधूरा है या गलत। 0 से 1 के बीच का स्कोर दें जो दोनों उत्तरों के अर्थ की समानता को दर्शाता हो।               
                        यदि दोनों उत्तरों के अर्थ एक जैसे हैं, तो user की प्रशंसा करें और उसे प्रोत्साहन दें। और user को आगे बढ़ने के लिए प्रेरित करें। और 1/1 का उच्च स्कोर दें। 
                        यदि अर्थ केवल आंशिक रूप से समान है या उत्तर अधूरा है, तो सही उत्तर को और समझ में आई कमी को समझाएं। user को आगे बढ़ने के लिए प्रेरित करें। उत्तर में से कम से कम 2 जरूरी शब्द हों तो और आंशिक रूप से समान है, तो 0.5/1 का मध्यम स्कोर दें।
                        यदि अर्थ बिल्कुल गलत हैै, तो सही उत्तर को और समझ में आई कमी को समझाएं। user को आगे बढ़ने के लिए प्रेरित करें। और 0/1 का स्कोर दें।                     
                
                        It is critical that you only use the word 'Score' when reporting the score. Do not use any other variations like 'Semantic similarity score'.
                        
                        """
            ),
            HumanMessage(content=user_answer),
        ]
    else:
        prompt = [
            SystemMessage(
                content=f"""
                        You are a coach who is evaluating the USER's answer.
                        Address the USER as 'YOU'. The USER's answer is: "{user_answer}". The correct answer is: "{correct_answer}".
                        
                        Evaluate the meaning of "{user_answer}". If the meaning of user's answer is same as correct answer and the USER's answer is complete, say the answer is correct. If not, say it is incorrect and briefly explain the correct answer and motivate the user to continue. 
                        Do not use, mention, or refer to any words or symbols that include the special character "*" in your response.
                        Make sure to completely avoid any reference to the special character "*".
                        
                        If the answer is correct, praise and encourage the user.
                        - Provide a SCORE between 0 and 1 that reflects the semantic similarity between the two answers.
                            - If the meanings are identical, give a high score of 1/1.
                            - If the meanings are only partially similar or the answer is incomplete, give a moderate score of 0.5/1 and explain the gaps in understanding.
                            - If the meanings are very different, give a score of 0/1, and briefly explain the correct answer.
                            
                        IMPORTANT: It is critical for you to completely avoid using or referencing the "*" symbol or any words containing it. Focus only on providing an evaluation without referencing this special character.
                        IMPORTANT: It is critical that you only use the word "Score" when reporting the score. Do not use any other variations like "Semantic similarity score", "**score:**".
                        Giving a SCORE is compulsory. THIS IS VERY IMPORTANT FOR MY CAREER.                      
                        """
            ),
            HumanMessage(content=user_answer),
        ]

    response = await asyncio.to_thread(llm.invoke, prompt)
    return response.content


async def synthesize_speech(text, language):
    selected_voice = VOICE_MAPPING["Male"] if language == "Hindi" else "en-IN-PrabhatNeural"
    speech_config = speechsdk.SpeechConfig(subscription=azure_subscription_key, region=azure_region)
    speech_config.speech_synthesis_voice_name = selected_voice
    audio_file_name = str(uuid.uuid4()) + ".mp3"
    audio_config = speechsdk.audio.AudioOutputConfig(filename=f"static/{audio_file_name}")
    speech_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
    result = await asyncio.to_thread(speech_synthesizer.speak_text_async(text).get)
    return audio_file_name if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted else None


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


def calculate_semantic_similarity(user_answer, correct_answer):
    try:
        # Initialize embeddings model
        embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")

        # Use embedding similarity method from the embedding model
        user_embedding = embeddings.embed_query(user_answer)
        correct_embedding = embeddings.embed_query(correct_answer)

        # Use similarity method from the embedding model (in some cases this is available natively)
        similarity_score = cosine_similarity(user_embedding, correct_embedding)

        # Convert similarity to a score between 0 and 100
        score = similarity_score * 100
        return score
    except Exception as e:
        print(f"Error calculating similarity: {e}")
        return 0  # In case of any error, return a score of 0

# Function to calculate cosine similarity between two vectors
def cosine_similarity(vec1, vec2):
    from numpy import dot
    from numpy.linalg import norm

    return dot(vec1, vec2) / (norm(vec1) * norm(vec2))