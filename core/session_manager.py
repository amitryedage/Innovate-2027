# session_manager.py — Multi-operator session lifecycle manager
# RESPONSIBILITY:
# Owns everything between "operator swipes card" and "PDF generated".
# Designed to loop indefinitely — when one operator ends their shift,
# this manager closes that session and prepares for the next operator
# without restarting the process or any thread.