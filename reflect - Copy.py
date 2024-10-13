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



reflect_bp = Blueprint('reflect', __name__)

api_key=os.environ['GOOGLE_API_KEY']
servamapi_key = os.getenv('SERVAM_API_KEY')
genai.configure(api_key=api_key)
azure_subscription_key = os.getenv("AZURE_SUBSCRIPTION_KEY")
azure_region = os.getenv("AZURE_REGION")
llm = ChatGoogleGenerativeAI(model="gemini-pro", convert_system_message_to_human=True, temperature=0.2)


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
                """
                hindi_greetings = [
                    f"नमस्ते! मैं आज आपका कोच हूँ। हम साथ में {product_name} के बारे में आपके ज्ञान को समझेंगे। कोई चिंता की बात नहीं, मैं यहाँ आपकी मदद के लिए हूँ। यह रहा आपका पहला प्रश्न।",
                    f"नमस्कार! आज हम {product_name} के बारे में आपकी समझ का परीक्षण करेंगे। मैं आपके साथ हूँ और हर कदम पर आपका मार्गदर्शन करूंगा। तो, शुरू करते हैं। यहाँ पहला प्रश्न है।",
                    f"आपका स्वागत है! मैं आपका कोच हूँ और आज हम {product_name} से जुड़ी कुछ बातें जानेंगे। आप तैयार हैं? तो चलिए शुरू करते हैं, यह रहा पहला सवाल।",
                    f"नमस्ते! मैं यहाँ हूँ आपकी मदद के लिए, ताकि हम मिलकर {product_name} के बारे में आपकी जानकारी को सुधारें। कोई भी संकोच मत कीजिए, यह रहा आपका पहला प्रश्न।",
                    f"नमस्कार! आज हम {product_name} पर आधारित आपके ज्ञान का मूल्यांकन करेंगे। चिंता मत कीजिए, मैं आपके साथ हूँ। शुरू करते हैं, यह रहा पहला सवाल।"
                ]
                """
                hindi_greetings = [
                    f"नमस्ते! "
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

            current_app.logger.info(f"Current question index: {current_question_index}, Correct Answer: {correct_answer}")

            prompt = (f"""
                      Correct only the misspelled words in "{user_answer}" based on the context of "{correct_answer}".
                      Do not add, remove, or change any other words or phrases beyond correcting spelling errors.                      
                    """)


            # AI LLM call to generate a human-like conversational response
            response = await asyncio.to_thread(llm.invoke, prompt)
            user_answer2 = response.content
            current_app.logger.info(f"user_answer 02: {user_answer2}")
            # Check if the response contains feedback indicating the answer is correct

            # Get feedback and score based on user's answer
            #feedback, score = await get_coach_feedback(user_answer, correct_answer, language)

            # Generate feedback for the current question
            feedback_text, score = await get_coach_feedback(user_answer2, correct_answer, language)

            # Check the response for correctness
            if "आपका उत्तर सही है" in feedback_text or "correct" in feedback_text:
                session['correct_answers'] += 1

            elif "आपका उत्तर आंशिक रूप से सही है" in feedback_text or "आपका उत्तर अधूरा है" in feedback_text or "incomplete" in feedback_text:
                session['correct_answers'] += .5

            else:
                print("The answer was marked incorrect.")

            # Update session score based on the feedback
            session['score'] += score

            current_app.logger.info(f"Coach feedback: {feedback_text}")
            current_app.logger.info(f"Coach Score: {session['correct_answers']}")


            feedback_audio_file_name = await synthesize_speech(feedback_text, language)
            if not feedback_audio_file_name:
                print(f"Error generating audio for feedback: {feedback_text}")
                return jsonify({"error": "Failed to generate audio for feedback."}), 500


            # Return feedback without proceeding to the next question
            return jsonify({
                "feedback_text": feedback_text,
                "feedback_audio": f"/static/{feedback_audio_file_name}",
                "correct_answer" : correct_answer,
                "user_answer2": user_answer2,
                "conversation_id": conversation_id,
                "is_final_feedback": False
            })

        elif action == 'next_question':
            # Provide the next question if available
            session['questions_asked'] += 1
            session.modified = True



            # Enforce the limit of 10 questions
            if session['questions_asked'] > session['total_questions']:
                # No more questions available, return final feedback
                final_score = session['correct_answers']
                final_feedback = generate_feedback(final_score, session['total_questions'])
                final_feedback_audio_file_name = await synthesize_speech(final_feedback, language)

                session.clear()  # End the quiz after feedback

                return jsonify({
                    "final_feedback_text": final_feedback,
                    "final_feedback_audio": f"/static/{final_feedback_audio_file_name}",
                    "conversation_id": conversation_id,
                    "is_final_feedback": True
                })

            # If more questions are available, serve the next question
            next_question_index = session['questions_asked'] - 1
            # Ensure the next question index is valid
            if next_question_index >= len(session['shuffled_questions']):
                return jsonify({"error": "No more questions available."}), 400

            # Get the next question
            next_question = session['shuffled_questions'][next_question_index]['question']
            next_question_audio_file_name = await synthesize_speech(next_question, language)

            return jsonify({
                "next_question_text": next_question,
                "next_question_audio": f"/static/{next_question_audio_file_name}",
                "conversation_id": conversation_id,
                "is_final_feedback": False
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
                        You are a professional question paper evaluator evaluating the user's answer. 
                        Use colloquial Hindi language. Address the user as 'आप'.

                        The user's answer is: "{user_answer}". The correct answer is: "{correct_answer}".

                        Compare the "{user_answer}" with the "{correct_answer}" and determine whether the accuracy and 
                        meaning of "{user_answer}" and "{correct_answer}" is similar and "{user_answer}" covers the 
                        key concepts of "{correct_answer}".                        

                        If the "{user_answer}" is similar in meaning to the "{correct_answer}" and covers most key 
                        concepts and important details, respond with 'आपका उत्तर सही है'. 
                        
                        If the "{user_answer}" is partially similar in meaning to the "{correct_answer}" and covers 
                        some key concepts but misses important details, say 'आपका उत्तर आंशिक रूप से सही है'. 
                        
                        If the "{user_answer}" is not similar in meaning to the "{correct_answer}" and misses
                        important details, say 'आपका उत्तर गलत है'.


                        If it is incomplete or incorrect, explain the correct answer briefly and encourage the user 
                        to move forward.
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

    # Calculate the similarity score
    similarity_score = calculate_semantic_similarity(user_answer, correct_answer)

    # Determine score based on similarity thresholds
    if similarity_score >= 90:
        score = 1  # Correct answer
    elif 60 <= similarity_score < 90:
        score = 0.5  # Incomplete answer
    else:
        score = 0  # Incorrect answer

    return response.content, score


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
def generate_feedback(score, total_questions):
    score_percentage = (score / total_questions) * 100
    score = f"Score: {score}/{total_questions} ({score_percentage:.2f}%)"

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