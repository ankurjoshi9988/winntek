# conversation_service.py
from datetime import datetime
from flask_login import current_user
from flask import session
from extensions import db
from models import Conversation, Message, Feedback, ReferConversation, ReferMessage, ReferFeedback
from langchain_google_genai import ChatGoogleGenerativeAI
import os
import google.generativeai as genai
from dotenv import load_dotenv
from googletrans import Translator
import asyncio
import psutil
import logging

# Load environment variables
load_dotenv()

# Set up Google API key
api_key = os.environ['GOOGLE_API_KEY']
genai.configure(api_key=api_key)
llm = ChatGoogleGenerativeAI(model="gemini-pro", convert_system_message_to_human=True)

MAX_QUERY_LENGTH = 500

# Translation function
async def translate_to_hindi(text):
    translator = Translator()
    translation = translator.translate(text, dest='hi')
    return translation.text

# Conversation functions
def start_conversation(user_id, persona):
    conversation = Conversation(user_id=user_id, persona=persona)
    db.session.add(conversation)
    db.session.commit()
    return conversation.id

def add_message(conversation_id, sender, content):
    message = Message(conversation_id=conversation_id, sender=sender, content=content)
    db.session.add(message)
    db.session.commit()

async def generate_overall_feedback(conversation):
    language = session.get('language', 'Hindi')  # Default to Hindi if not set

    formatted_conversation = "\n".join([f"{'Customer' if msg.sender == 'system' else 'Agent'}: {msg.content}" for msg in conversation.messages])

    overall_prompt = (
        f"Based on the following conversation between an insurance agent and a customer, provide feedback in {'Hindi' if language == 'Hindi' else 'English'} on the agent's performance. "
        "The feedback should be categorized as either 'Positives' or 'Needs Improvement' only if necessary and include specific comments on how the agent handled the conversation."
        f"\n\nConversation:\n{formatted_conversation}\n\nOverall Feedback:"
    )

    log_system_usage("Before overall feedback generation")

    overall_response = await llm_invoke(overall_prompt)
    overall_feedback = overall_response.content if overall_response else "Could not generate feedback at this time."

    log_system_usage("After overall feedback generation")

    processed_feedback = process_feedback(overall_feedback)

    # Translate if necessary
    if language == 'Hindi':
        translated_chunk_text = await translate_to_hindi(processed_feedback)
        final_feedback = translated_chunk_text + "\n"
    else:
        final_feedback = processed_feedback + "\n"

    log_system_usage("After translating overall feedback")

    return final_feedback

async def generate_feedback(conversation):
    language = session.get('language', 'Hindi')  # Default to Hindi if not set
    if not conversation or not conversation.messages:
        return "Feedback could not be generated due to missing conversation details."

    overall_feedback = await generate_overall_feedback(conversation)

    individual_feedback_list = []
    for message in conversation.messages:
        if message.sender == 'user':
            individual_prompt = (
                f"Provide feedback on the following response from the agent in {'simple Hindi' if language == 'Hindi' else 'simple English'} language. "
                "Indicate whether it was 'Positive' or 'Needs Improvement' only if necessary and provide specific comments on how it could be improved if needed."
                f"\n\nYour response: {message.content}\n\nFeedback:"
            )

            individual_response = await llm_invoke(individual_prompt)
            feedback_text = individual_response.content if individual_response else "Could not generate individual feedback at this time."

            if language == 'Hindi':
                translated_feedback_text = await translate_to_hindi(feedback_text)
                individual_feedback_list.append(f"आपका जवाब: {message.content}\nफ़ीडबैक: {translated_feedback_text}")
            else:
                individual_feedback_list.append(f"Your response: {message.content}\nFeedback: {feedback_text}")

    combined_feedback = (
        f"{'कुल फ़ीडबैक:' if language == 'Hindi' else 'Overall Feedback:'}\n{overall_feedback}\n\n"
        f"{'व्यक्तिगत फ़ीडबैक:' if language == 'Hindi' else 'Individual Feedback:'}\n" + "\n\n".join(individual_feedback_list)
    )

    return combined_feedback

def log_system_usage(context=""):
    """Log system usage statistics."""
    process = psutil.Process()
    memory_info = process.memory_info()
    cpu_usage = process.cpu_percent(interval=1)
    logging.debug(f"{context} - Memory Usage: RSS={memory_info.rss / 1024 ** 2:.2f} MB, "
                  f"VMS={memory_info.vms / 1024 ** 2:.2f} MB, CPU Usage={cpu_usage:.2f}%")

def process_feedback(feedback):
    """Process feedback to limit to specific points."""
    lines = feedback.split('\n')
    positives, improvements = [], []
    current_section = None

    for line in lines:
        if 'Positives' in line:
            current_section = positives
        elif 'Needs Improvement' in line:
            current_section = improvements
        elif current_section is not None and line.strip():
            current_section.append(line)

    return '\n'.join([
        "Positives:" + "\n" + "\n".join(positives[:1]) + "\n"
        "Needs Improvement:" + "\n" + "\n".join(improvements[:1])
    ])

async def llm_invoke(prompt):
    """Invoke the LLM asynchronously."""
    response = await asyncio.to_thread(llm.invoke, prompt)
    return response

async def close_conversation(app, conversation_id):
    """Close the conversation and generate feedback."""
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        app.logger.error("No conversation found with the given ID: %s", conversation_id)
        return "No conversation found with the given ID."

    existing_feedback = Feedback.query.filter_by(conversation_id=conversation_id).first()
    if existing_feedback:
        app.logger.debug("Returning existing feedback for conversation_id: %s", conversation_id)
        return existing_feedback.content

    try:
        feedback_content = await generate_feedback(conversation)
        feedback = Feedback(conversation_id=conversation_id, content=feedback_content)
        db.session.add(feedback)
        db.session.commit()
        app.logger.debug("Feedback generated and saved for conversation_id: %s", conversation_id)
        return feedback_content
    except Exception as e:
        app.logger.error(f"An error occurred while closing the conversation {conversation_id}: {e}")
        db.session.rollback()
        return f"An error occurred while closing the conversation: {str(e)}"

def get_past_conversations(user_id):
    """Retrieve past conversations for a user."""
    conversations = Conversation.query.filter_by(user_id=user_id).all()
    past_conversations = []

    for convo in conversations:
        feedback = Feedback.query.filter_by(conversation_id=convo.id).first()
        past_conversations.append({
            'conversation_id': convo.id,
            'persona': convo.persona,
            'created_at': convo.created_at,
            'messages': [{'sender': msg.sender, 'content': msg.content, 'timestamp': msg.timestamp} for msg in convo.messages],
            'feedback': feedback.content if feedback else 'No feedback available'
        })

    return past_conversations

# Reflect functions
def start_refer_conversation(user_id, product_id):
    """Start a referral conversation."""
    conversation = ReferConversation(user_id=user_id, product_id=product_id)
    db.session.add(conversation)
    db.session.commit()
    return conversation.id

def add_refer_message(conversation_id, sender, content):
    """Add a message to a referral conversation."""
    message = ReferMessage(conversation_id=conversation_id, sender=sender, content=content)
    db.session.add(message)
    db.session.commit()

async def generate_refer_feedback(conversation):
    """Generate feedback for a referral conversation."""
    formatted_conversation = "\n".join(
        [f"{'Coach' if msg.sender == 'system' else 'User'}: {msg.content}" for msg in conversation.messages])

    feedback_prompt = (
        "Based on the following conversation about the selected product, provide feedback on the user's knowledge. "
        "Assess how well the user understood the product's features and benefits. Provide a score from 0-100 and categorize the performance as Beginner, Competent, Proficient, or Expert. "
        f"\n\nConversation:\n{formatted_conversation}\n\nFeedback:"
    )

    feedback_response = await llm_invoke(feedback_prompt)
    feedback_content = feedback_response.content if feedback_response else "Could not generate feedback."

    # Process feedback and store
    score, category = process_refer_feedback(feedback_content)

    feedback = ReferFeedback(conversation_id=conversation.id, content=feedback_content, score=score, category=category)
    db.session.add(feedback)
    db.session.commit()

    return feedback_content

def process_refer_feedback(feedback_content):
    """Extract score and category from referral feedback."""
    # Extract score and category from feedback (this is just a basic example; you can refine this logic)
    score = 80  # Default example score
    category = 'Proficient'  # Example category

    # Implement logic to parse feedback_content and extract the score and category
    # ...

    return score, category
