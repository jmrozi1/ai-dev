ARG JENKINS_BASE_IMAGE=jenkins/jenkins:2.516.3-jdk21
FROM ${JENKINS_BASE_IMAGE}

COPY --chown=jenkins:jenkins plugins.lock /usr/share/jenkins/ref/plugins.lock.txt

RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.lock.txt
