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
from translate import Translator
from googletrans import Translator
import asyncio
from concurrent.futures import ThreadPoolExecutor
import textwrap
import logging
import psutil
import time


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

async def generate_overall_feedback(conversation):
    language = session.get('language', 'Hindi')  # Default to Hindi if not set

    print("feedback language",language)
    formatted_conversation = "\n".join([f"{'Customer' if msg.sender == 'system' else 'Agent'}: {msg.content}" for msg in conversation.messages])
    print(f"Formatted conversation: {formatted_conversation}")
    print(f"Selected language: {language}")

    if language == 'Hindi':
        overall_prompt = (
            "Based on the following conversation between an insurance agent and a customer, provide feedback in Hindi language on the agent's performance. "
            "The feedback should be categorized as either 'Positives' or 'Needs Improvement' only if necessary and include specific comments on how the agent handled the conversation."
            "Consider the overall chat conversation as context. The feedback should reflect how the conversation started, how the agent responded to queries, and how the conversation ended. Do not generate or write '***' in feedback text.\n\n"
            f"Conversation:\n{formatted_conversation}\n\nOverall Feedback:"
        )
    else:
        overall_prompt = (
            "Based on the following conversation between an insurance agent and a customer, provide feedback in English language on the agent's performance. "
            "The feedback should be categorized as either 'Positives' or 'Needs Improvement' only if necessary and include specific comments on how the agent handled the conversation."
            "Consider the overall chat conversation as context. The feedback should reflect how the conversation started, how the agent responded to queries, and how the conversation ended. Do not generate or write '***' in feedback text.\n\n"
            f"Conversation:\n{formatted_conversation}\n\nOverall Feedback:"
        )

    log_system_usage("Before overall feedback generation")

    overall_response = await llm_invoke(overall_prompt)
    print(f"Overall response: {overall_response}")  # Add this line
    overall_feedback = overall_response.content if overall_response else "Could not generate feedback at this time."

    log_system_usage("After overall feedback generation")
    print(f"Raw overall feedback: {overall_feedback}")  # Add this line

    # Process the feedback to limit it to 2 points per category
    processed_feedback = process_feedback(overall_feedback)
    print(f"Processed feedback: {processed_feedback}")  # Add this line

    log_system_usage("After processing overall feedback")

    # Translate the overall feedback only if it's in English and language is set to Hindi
    if language == 'Hindi':
        translated_chunk_text = await translate_to_hindi(processed_feedback)
        print(f"Translated feedback: {translated_chunk_text}")  # Add this line
        final_feedback = translated_chunk_text + "\n"
    else:
        final_feedback = processed_feedback + "\n"

    log_system_usage("After translating overall feedback")

    return final_feedback



async def generate_feedback(conversation):
    language = session.get('language', 'Hindi')  # Default to Hindi if not set
    if not conversation or not conversation.messages:
        return "Feedback could not be generated due to missing conversation details."

    # Generate overall feedback
    overall_feedback = await generate_overall_feedback(conversation)

    # If overall feedback is not generated, provide a placeholder
    if not overall_feedback:
        overall_feedback = "No overall feedback available."

    log_system_usage("Before individual feedback generation")

    # Generate individual feedback
    individual_feedback_list = []
    for message in conversation.messages:
        if message.sender == 'user':
            if language == 'Hindi':
                individual_prompt = (
                    "Provide feedback on the following response from the agent in simple Hindi language. "
                    "Indicate whether it was 'Positive' or 'Needs Improvement' only if necessary and provide specific comments on how it could be improved if needed. These indicators should be in English."
                    "Consider the overall chat conversation as context. Do not generate '***' in feedback text.\n\n"
                    f"Your response: {message.content}\n\nFeedback:"
                )
            else:
                individual_prompt = (
                    "Provide feedback on the following response from the agent in simple English language. "
                    "Indicate whether it was 'Positive' or 'Needs Improvement' only if necessary and provide specific comments on how it could be improved if needed."
                    "Consider the overall chat conversation as context. Do not generate '***' in feedback text.\n\n"
                    f"Your response: {message.content}\n\nFeedback:"
                )

            individual_response = await llm_invoke(individual_prompt)
            feedback_text = individual_response.content if individual_response else "Could not generate individual feedback at this time."

            # Translate individual feedback only if language is Hindi
            if language == 'Hindi':
                translated_feedback_text = await translate_to_hindi(feedback_text)
                individual_feedback_list.append(f"आपका जवाब: {message.content}\nफ़ीडबैक: {translated_feedback_text}")
            else:
                individual_feedback_list.append(f"Your response: {message.content}\nFeedback: {feedback_text}")

    log_system_usage("After individual feedback generation")

    # Combine feedback
    if language == 'Hindi':
        combined_feedback = f"कुल फ़ीडबैक:\n{overall_feedback}\n\nव्यक्तिगत फ़ीडबैक:\n" + "\n\n".join(
            individual_feedback_list)
    else:
        combined_feedback = f"Overall Feedback:\n{overall_feedback}\n\nIndividual Feedback:\n" + "\n\n".join(
            individual_feedback_list)

    log_system_usage("After generating combined feedback")

    return combined_feedback


def log_system_usage(context=""):
    process = psutil.Process()
    memory_info = process.memory_info()
    cpu_usage = process.cpu_percent(interval=1)
    logging.debug(f"{context} - Memory Usage: RSS={memory_info.rss / 1024 ** 2:.2f} MB, VMS={memory_info.vms / 1024 ** 2:.2f} MB, CPU Usage={cpu_usage:.2f}%")

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


#--------------------------------------------------------------------------------------
#Reflect

def start_refer_conversation(user_id, product_id):
    conversation = ReferConversation(user_id=user_id, product_id=product_id)
    db.session.add(conversation)
    db.session.commit()
    return conversation.id

def add_refer_message(conversation_id, sender, content):
    message = ReferMessage(conversation_id=conversation_id, sender=sender, content=content)
    db.session.add(message)
    db.session.commit()


async def generate_refer_feedback(conversation):
    # Fetch the conversation messages
    formatted_conversation = "\n".join(
        [f"{'Coach' if msg.sender == 'system' else 'User'}: {msg.content}" for msg in conversation.messages])

    # Product-related feedback prompt
    feedback_prompt = (
        "Based on the following conversation about the selected product, provide feedback on the user's knowledge. "
        "Assess how well the user understood the product's features and benefits. Provide a score from 0-100 and categorize the performance as Beginner, Competent, Proficient, or Expert. "
        "Here’s the conversation:\n\n"
        f"Conversation:\n{formatted_conversation}\n\nFeedback:"
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
    # Extract score and category from feedback (this is just a basic example; you can refine this logic)
    score = 80  # Default example score
    category = 'Proficient'  # Example category

    # Implement logic to analyze the feedback and extract actual score/category if needed.

    return score, category

