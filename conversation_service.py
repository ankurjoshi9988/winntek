# conversation_service.py
from datetime import datetime
from flask_login import current_user
from extensions import db
from models import Conversation, Message, Feedback
from langchain_google_genai import ChatGoogleGenerativeAI
import os
import google.generativeai as genai
from dotenv import load_dotenv
from translate import Translator
from googletrans import Translator
import asyncio
from concurrent.futures import ThreadPoolExecutor
import textwrap
import logging


# Configure the root logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)  # Set to INFO or WARNING

# Configure other specific loggers
logging.getLogger('hpack').setLevel(logging.WARNING)  # Example: Set hpack logs to WARNING
logging.getLogger('httpx').setLevel(logging.WARNING)  # Example: Set httpx logs to WARNING


MAX_QUERY_LENGTH = 500

load_dotenv()

api_key=os.environ['GOOGLE_API_KEY']
genai.configure(api_key=api_key)
llm = ChatGoogleGenerativeAI(model="gemini-pro", convert_system_message_to_human=True)

"""async def translate_to_hindi(text):
    translator = Translator(to_lang="hi")
    translation = translator.translate(text)
    return translation
"""
async def translate_to_hindi(text):
    translator = Translator()
    translation = translator.translate(text, dest='hi')
    return translation.text


def start_conversation(user_id, persona):
    conversation = Conversation(user_id=user_id, persona=persona)
    db.session.add(conversation)
    db.session.commit()
    return conversation.id




def add_message(conversation_id, sender, content):

    message = Message(conversation_id=conversation_id, sender=sender, content=content)
    db.session.add(message)
    db.session.commit()


async def generate_feedback(conversation):
    if not conversation or not conversation.messages:
        return "Feedback could not be generated due to missing conversation details."

    formatted_conversation = ""
    for message in conversation.messages:
        sender = "Customer" if message.sender == 'system' else "Agent"
        formatted_conversation += f"{sender}: {message.content}\n"

    overall_prompt = (
        "Based on the following conversation between an insurance agent and a customer, provide feedback in Hindi language on the agent's performance. "
        "The feedback should be categorized as either 'Positives' or 'Needs Improvement' only if necessary and include specific comments on how the agent handled the conversation."
        "Consider the overall chat conversation as context. The feedback should reflect how the conversation started, how the agent responded to queries, and how the conversation ended. Do not generate or write '***' in feedback text.\n\n"
        f"Conversation:\n{formatted_conversation}\n\nOverall Feedback:"
    )

    overall_response = await llm_invoke(overall_prompt)
    overall_feedback = overall_response.content if overall_response else "Could not generate feedback at this time."

    # Process the feedback to limit it to 2 points per category
    overall_feedback_processed = process_feedback(overall_feedback)

    # Translate the processed feedback to Hindi
    translated_feedback = await translate_to_hindi(overall_feedback_processed)

    individual_feedback_list = []
    for message in conversation.messages:
        if message.sender == 'user':
            individual_prompt = (
                "Provide feedback on the following response from the agent in simple Hindi language. "
                "Indicate whether it was 'Positive' or 'Needs Improvement' only if necessary and provide specific comments on how it could be improved if needed. These indicators should be in English."
                "Consider the overall chat conversation as context. Do not generate '***' in feedback text.\n\n"
                f"Overall Conversation Context:\n{formatted_conversation}\n\n"
                f"Agent's Response: {message.content}\n\nFeedback:"
            )

            individual_response = await llm_invoke(individual_prompt)
            feedback_text = individual_response.content if individual_response else "Could not generate individual feedback at this time."
            translated_feedback_text = await translate_to_hindi(feedback_text)
            individual_feedback_list.append(f"आपका जवाब: {message.content}\nफ़ीडबैक: {translated_feedback_text}")

    combined_feedback = f"कुल फ़ीडबैक:\n{translated_feedback}\n\nव्यक्तिगत फ़ीडबैक:\n" + "\n\n".join(
        individual_feedback_list)

    return combined_feedback


def process_feedback(feedback):
    lines = feedback.split('\n')
    positives = []
    improvements = []
    current_section = None

    for line in lines:
        if 'Positives' in line:
            current_section = positives
        elif 'Needs Improvement' in line:
            current_section = improvements
        elif current_section is not None and line.strip():
            current_section.append(line)

    positives = positives[:1]
    improvements = improvements[:1]

    result = ["Positives:"]
    result.extend(positives)
    result.append("Needs Improvement:")
    result.extend(improvements)

    return '\n'.join(result)


async def llm_invoke(prompt):
    response = await asyncio.to_thread(llm.invoke, prompt)
    return response


async def close_conversation(app, conversation_id):
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        app.logger.error("No conversation found with the given ID: %s", conversation_id)
        return "No conversation found with the given ID."

    existing_feedback = Feedback.query.filter_by(conversation_id=conversation_id).first()
    if existing_feedback:
        app.logger.debug("Returning existing feedback for conversation_id: %s", conversation_id)
        return existing_feedback.content  # Return the existing feedback if it exists

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



